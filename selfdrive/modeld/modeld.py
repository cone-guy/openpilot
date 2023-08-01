import time
import logging
import numpy as np
from typing import Dict, Optional
from cereal.messaging import PubMaster, SubMaster
from cereal.visionipc import VisionIpcClient, VisionStreamType, VisionBuf
from common.filter_simple import FirstOrderFilter
from common.realtime import set_core_affinity, set_realtime_priority
from common.transformations.model import medmodel_frame_from_calib_frame, sbigmodel_frame_from_calib_frame
from common.transformations.camera import view_frame_from_device_frame, tici_fcam_intrinsics, tici_ecam_intrinsics
from common.transformations.orientation import rot_from_euler
from selfdrive.modeld.models.cl_pyx import CLContext # pylint: disable=no-name-in-module
from selfdrive.modeld.runners.runmodel_pyx import ONNXModel, Runtime # pylint: disable=no-name-in-module
from selfdrive.modeld.models.commonmodel_pyx import ModelFrame # pylint: disable=no-name-in-module
from system.hardware import PC

FEATURE_LEN = 128
HISTORY_BUFFER_LEN = 99
DESIRE_LEN = 8
TRAFFIC_CONVENTION_LEN = 2
DRIVING_STYLE_LEN = 12
NAV_FEATURE_LEN = 256
OUTPUT_SIZE = 5990
MODEL_OUTPUT_SIZE = 6120
MODEL_FREQ = 20

MODEL_WIDTH = 512
MODEL_HEIGHT = 256
MODEL_FRAME_SIZE = MODEL_WIDTH * MODEL_HEIGHT * 3 // 2
BUF_SIZE = MODEL_FRAME_SIZE * 2

# NOTE: These are almost exactly the same as the numbers in modeld.cc, but to get perfect equivalence we might have to copy them exactly
calib_from_medmodel = np.linalg.inv(medmodel_frame_from_calib_frame[:, :3])
calib_from_sbigmodel = np.linalg.inv(sbigmodel_frame_from_calib_frame[:, :3])

def update_calibration(device_from_calib_euler:np.ndarray, wide_camera:bool, bigmodel_frame:bool) -> np.ndarray:
  cam_intrinsics = tici_ecam_intrinsics if wide_camera else tici_fcam_intrinsics
  calib_from_model = calib_from_sbigmodel if bigmodel_frame else calib_from_medmodel
  device_from_calib = rot_from_euler(device_from_calib_euler)
  camera_from_calib = cam_intrinsics @ view_frame_from_device_frame @ device_from_calib
  warp_matrix: np.ndarray = camera_from_calib @ calib_from_model
  return warp_matrix

class ModelState:
  frame: ModelFrame
  wide_frame: ModelFrame
  inputs: Dict[str, np.ndarray]
  output: np.ndarray
  prev_desire: np.ndarray  # for tracking the rising edge of the pulse
  model: ONNXModel

  def __init__(self, context:CLContext):
    self.frame = ModelFrame(context)
    self.wide_frame = ModelFrame(context)
    self.prev_desire = np.zeros(DESIRE_LEN, dtype=np.float32)
    self.output = np.zeros(MODEL_OUTPUT_SIZE, dtype=np.float32)
    self.inputs = {
      'desire_pulse': np.zeros(DESIRE_LEN * (HISTORY_BUFFER_LEN+1), dtype=np.float32),
      'traffic_convention': np.zeros(TRAFFIC_CONVENTION_LEN, dtype=np.float32),
      'nav_features': np.zeros(NAV_FEATURE_LEN, dtype=np.float32),
      'feature_buffer': np.zeros(HISTORY_BUFFER_LEN * FEATURE_LEN, dtype=np.float32),
    }

    self.model = ONNXModel("models/supercombo.onnx", self.output, Runtime.GPU, False, context)
    self.model.addInput("input_imgs", None)
    self.model.addInput("big_input_imgs", None)
    for k,v in self.inputs.items():
      self.model.addInput(k, v)

  def eval(self, buf:VisionBuf, wbuf:VisionBuf, transform:np.ndarray, transform_wide:np.ndarray, inputs:Dict[str, np.ndarray], prepare_only:bool) -> Optional[np.ndarray]:
    # Model decides when action is completed, so desire input is just a pulse triggered on rising edge
    inputs['desire_pulse'][0] = 0
    self.inputs['desire_pulse'][:-DESIRE_LEN] = self.inputs['desire_pulse'][DESIRE_LEN:]
    self.inputs['desire_pulse'][-DESIRE_LEN:] = np.where(inputs['desire_pulse'] - self.prev_desire > .99, inputs['desire_pulse'], 0)
    self.prev_desire[:] = inputs['desire_pulse']
    logging.info("Desire enqueued")

    self.inputs['traffic_convention'][:] = inputs['traffic_convention']
    self.inputs['nav_features'][:] = inputs['nav_features']
    # self.inputs['driving_style'][:] = inputs['driving_style']

    # if getCLBuffer is not None, frame will be None
    frame = self.frame.prepare(buf, transform.astype(np.float32).flatten(), self.model.getCLBuffer("input_imgs"))
    self.model.setInputBuffer("input_imgs", frame)
    logging.info("Image added")

    if wbuf is not None:
      wide_frame = self.wide_frame.prepare(wbuf, transform_wide.astype(np.float32).flatten(), self.model.getCLBuffer("big_input_imgs"))
      self.model.setInputBuffer("big_input_imgs", wide_frame)
      logging.info("Extra image added")

    if prepare_only:
      return None

    self.model.execute()
    logging.info("Execution finished")

    self.inputs['feature_buffer'][:-FEATURE_LEN] = self.inputs['feature_buffer'][FEATURE_LEN:]
    self.inputs['feature_buffer'][-FEATURE_LEN:] = self.output[OUTPUT_SIZE:OUTPUT_SIZE+FEATURE_LEN]
    logging.info("Features enqueued")

    return self.output



if __name__ == '__main__':
  if not PC:
    set_realtime_priority(54)
    set_core_affinity([7])

  cl_context = CLContext()
  model = ModelState(cl_context)
  logging.warning("models loaded, modeld starting")

  # visionipc clients
  while True:
    available_streams = VisionIpcClient.available_streams("camerad", block=False)
    if available_streams:
      use_extra_client = VisionStreamType.VISION_STREAM_WIDE_ROAD in available_streams and VisionStreamType.VISION_STREAM_ROAD in available_streams
      main_wide_camera = VisionStreamType.VISION_STREAM_ROAD not in available_streams
      break
    time.sleep(.1)

  vipc_client_main_stream = VisionStreamType.VISION_STREAM_WIDE_ROAD if main_wide_camera else VisionStreamType.VISION_STREAM_ROAD
  vipc_client_main = VisionIpcClient("camerad", vipc_client_main_stream, True)
  vipc_client_extra = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_WIDE_ROAD, False)
  logging.warning(f"vision stream set up, main_wide_camera: {main_wide_camera}, use_extra_client: {use_extra_client}")

  # TODO: Is it safe to use blocking=True here?
  while not vipc_client_main.connect(False):
    time.sleep(0.1)
  while not vipc_client_extra.connect(False):
    time.sleep(0.1)

  logging.warning(f"connected main cam with buffer size: {vipc_client_main.buffer_len} ({vipc_client_main.width} x {vipc_client_main.height})")
  if use_extra_client:
    logging.warning(f"connected extra cam with buffer size: {vipc_client_extra.buffer_len} ({vipc_client_extra.width} x {vipc_client_extra.height})")

  # messaging
  pm = PubMaster(["modelV2", "cameraOdometry"])
  sm = SubMaster(["lateralPlan", "roadCameraState", "liveCalibration", "driverMonitoringState"])

  # setup filter to track dropped frames
  # TODO: I don't think the python version of FirstOrderFilter matches the c++ version exactly
  frame_dropped_filter = FirstOrderFilter(0., 10., 1. / MODEL_FREQ)
  frame_id = 0
  last_vipc_frame_id = 0
  last = 0.0
  run_count = 0

  model_transform_main = np.zeros((3, 3), dtype=np.float32)
  model_transform_extra = np.zeros((3, 3), dtype=np.float32)
  live_calib_seen = False
  driving_style = np.array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0], dtype=np.float32)
  nav_features = np.zeros(NAV_FEATURE_LEN, dtype=np.float32)
  buf_main = None
  buf_extra = None

  while True:
    # Keep receiving frames until we are at least 1 frame ahead of previous extra frame
    while vipc_client_main.timestamp_sof < vipc_client_extra.timestamp_sof + 25000000:
      buf_main = vipc_client_main.recv()
      if buf_main is None:
        break

    if buf_main is None:
      logging.error("vipc_client_main no frame")
      continue

    if use_extra_client:
      # Keep receiving extra frames until frame id matches main camera
      while True:
        buf_extra = vipc_client_extra.recv()
        if buf_extra is None or vipc_client_main.timestamp_sof < vipc_client_extra.timestamp_sof + 25000000:
          break

      if buf_extra is None:
        logging.error("vipc_client_extra no frame")
        continue

      if abs(vipc_client_main.timestamp_sof - vipc_client_main.timestamp_sof) > 10000000:
        logging.error("frames out of sync! main: {} ({:.5f}), extra: {} ({:.5f})".format(
          vipc_client_main.frame_id, vipc_client_main.timestamp_sof / 1e9,
          vipc_client_extra.frame_id, vipc_client_extra.timestamp_sof / 1e9))

    else:
      # Use single camera
      buf_extra = buf_main

    # TODO: path planner timeout?
    sm.update(0)
    desire = sm["lateralPlan"].desire.raw
    is_rhd = sm["driverMonitoringState"].isRHD
    frame_id = sm["roadCameraState"].frameId
    if sm.updated["liveCalibration"]:
      device_from_calib_euler = np.array(sm["liveCalibration"].rpyCalib)
      model_transform_main = update_calibration(device_from_calib_euler, main_wide_camera, False)
      model_transform_extra = update_calibration(device_from_calib_euler, True, True)
      live_calib_seen = True

    traffic_convention = np.zeros(2)
    traffic_convention[int(is_rhd)] = 1

    vec_desire = np.zeros(DESIRE_LEN, dtype=np.float32)
    if desire >= 0 and desire < DESIRE_LEN:
      vec_desire[desire] = 1

    # tracked dropped frames
    vipc_dropped_frames = vipc_client_main.frame_id - last_vipc_frame_id - 1
    frames_dropped = frame_dropped_filter.update(min(vipc_dropped_frames, 10))
    if run_count < 10: # let frame drops warm up
      # frame_dropped_filter.reset(0)
      frames_dropped = 0.
    run_count = run_count + 1

    frame_drop_ratio = frames_dropped / (1 + frames_dropped)
    prepare_only = vipc_dropped_frames > 0
    if prepare_only:
      logging.error(f"skipping model eval. Dropped {vipc_dropped_frames} frames")

    inputs:Dict[str, np.ndarray] = {
      'desire_pulse': vec_desire,
      'traffic_convention': traffic_convention,
      'driving_style': driving_style,
      'nav_features': nav_features}

    mt1 = time.perf_counter()
    model_output = model.eval(buf_main, buf_extra, model_transform_main, model_transform_extra, inputs, prepare_only)
    mt2 = time.perf_counter()
    model_execution_time = mt2 - mt1

    """
    if model_output:
      model_publish(pm, vipc_client_main.frame_id, vipc_client_extra.frame_id, frame_id, frame_drop_ratio, *model_output, vipc_client_main.timestamp_eof, model_execution_time,
                    kj::ArrayPtr<const float>(model.output.data(), model.output.size()), live_calib_seen)
      posenet_publish(pm, vipc_client_main.frame_id, vipc_dropped_frames, *model_output, vipc_client_main.timestamp_eof, live_calib_seen)
    """

    # print("model process: %.2fms, from last %.2fms, vipc_frame_id %u, frame_id, %u, frame_drop %.3f\n" % (mt2 - mt1, mt1 - last, extra.frame_id, frame_id, frame_drop_ratio))
    last = mt1
    last_vipc_frame_id = vipc_client_main.frame_id
