#!/usr/bin/env python3
"""
Scripts to drive a donkey 2 car

Usage:
    manage.py (drive) [--model=<model>] [--js] [--type=(linear|categorical)] [--camera=(single|stereo)] [--meta=<key:value> ...] [--myconfig=<filename>]
    manage.py (train) [--tubs=tubs] (--model=<model>) [--type=(linear|inferred|tensorrt_linear|tflite_linear)]

Options:
    -h --help               Show this screen.
    --js                    Use physical joystick.
    -f --file=<file>        A text file containing paths to tub files, one per line. Option may be used more than once.
    --meta=<key:value>      Key/Value strings describing describing a piece of meta data about this drive. Option may be used more than once.
    --myconfig=filename     Specify myconfig file to use. 
                            [default: myconfig.py]
"""
import os
import time
import logging
from docopt import docopt

#
# import cv2 early to avoid issue with importing after tensorflow
# see https://github.com/opencv/opencv/issues/14884#issuecomment-599852128
#
try:
    import cv2
except:
    pass


from tensorflow.python.ops.linalg_ops import norm


import donkeycar as dk
from donkeycar.parts.transform import TriggeredCallback, DelayedTrigger
from donkeycar.parts.tub_v2 import TubWriter
from donkeycar.parts.datastore import TubHandler
from donkeycar.parts.controller import LocalWebController, WebFpv, JoystickController
from donkeycar.parts.throttle_filter import ThrottleFilter
from donkeycar.parts.behavior import BehaviorPart
from donkeycar.parts.file_watcher import FileWatcher
from donkeycar.parts.launch import AiLaunch
from donkeycar.parts.velocity import StepSpeedController
from donkeycar.parts.velocity import VelocityNormalize, VelocityUnnormalize
from donkeycar.parts.kinematics import NormalizeSteeringAngle, UnnormalizeSteeringAngle, TwoWheelSteeringThrottle
from donkeycar.parts.kinematics import Unicycle, InverseUnicycle, UnicycleUnnormalizeAngularVelocity, UnicycleNormalizeAngularVelocity
from donkeycar.parts.kinematics import Bicycle, InverseBicycle, BicycleUnnormalizeAngularVelocity, BicycleNormalizeAngularVelocity
from donkeycar.parts import pins;

from donkeycar.utils import *

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def drive(cfg, model_path=None, use_joystick=False, model_type=None,
          camera_type='single', meta=[]):
    """
    Construct a working robotic vehicle from many parts. Each part runs as a
    job in the Vehicle loop, calling either it's run or run_threaded method
    depending on the constructor flag `threaded`. All parts are updated one
    after another at the framerate given in cfg.DRIVE_LOOP_HZ assuming each
    part finishes processing in a timely manner. Parts may have named outputs
    and inputs. The framework handles passing named outputs to parts
    requesting the same named input.
    """
    logger.info(f'PID: {os.getpid()}')
    if cfg.DONKEY_GYM:
        #the simulator will use cuda and then we usually run out of resources
        #if we also try to use cuda. so disable for donkey_gym.
        os.environ["CUDA_VISIBLE_DEVICES"]="-1"

    if model_type is None:
        if cfg.TRAIN_LOCALIZER:
            model_type = "localizer"
        elif cfg.TRAIN_BEHAVIORS:
            model_type = "behavior"
        else:
            model_type = cfg.DEFAULT_MODEL_TYPE

    is_velocity_model = model_type.endswith("velocity")
    have_speed_control = cfg.HAVE_ODOM and is_velocity_model
    is_differential_drive = cfg.DRIVE_TRAIN_TYPE.startswith("DC_TWO_WHEEL")

    #Initialize car
    V = dk.vehicle.Vehicle()

    #Initialize logging before anything else to allow console logging
    if cfg.HAVE_CONSOLE_LOGGING:
        logger.setLevel(logging.getLevelName(cfg.LOGGING_LEVEL))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(cfg.LOGGING_FORMAT))
        logger.addHandler(ch)

    if cfg.HAVE_MQTT_TELEMETRY:
        from donkeycar.parts.telemetry import MqttTelemetry
        tel = MqttTelemetry(cfg)

    #
    # setup encoders, odometry and pose estimation
    #
    add_odometry(V, cfg)


    #
    # setup primary camera
    #
    add_camera(V, cfg, camera_type)


    # add lidar
    if cfg.USE_LIDAR:
        from donkeycar.parts.lidar import RPLidar
        if cfg.LIDAR_TYPE == 'RP':
            print("adding RP lidar part")
            lidar = RPLidar(lower_limit = cfg.LIDAR_LOWER_LIMIT, upper_limit = cfg.LIDAR_UPPER_LIMIT)
            V.add(lidar, inputs=[],outputs=['lidar/dist_array'], threaded=True)
        if cfg.LIDAR_TYPE == 'YD':
            print("YD Lidar not yet supported")

    if cfg.SHOW_FPS:
        from donkeycar.parts.fps import FrequencyLogger
        V.add(FrequencyLogger(cfg.FPS_DEBUG_INTERVAL),
              outputs=["fps/current", "fps/fps_list"])

    #
    # add the user input controller(s)
    # - this will add the web controller
    # - it will optionally add any configured 'joystick' controller
    #
    has_input_controller = hasattr(cfg, "CONTROLLER_TYPE") and cfg.CONTROLLER_TYPE != "mock"
    ctr = add_user_controller(V, cfg, use_joystick)


    #this throttle filter will allow one tap back for esc reverse
    th_filter = ThrottleFilter()
    V.add(th_filter, inputs=['user/throttle'], outputs=['user/throttle'])

    #See if we should even run the pilot module.
    #This is only needed because the part run_condition only accepts boolean
    class PilotCondition:
        def run(self, mode):
            if mode == 'user':
                return False
            else:
                return True

    V.add(PilotCondition(), inputs=['user/mode'], outputs=['run_pilot'])

    # determine if we should be using speed control
    class SpeedControlCondition:
        def __init__(self, have_speed_control) -> None:
            self.have_speed_control = have_speed_control

        def run(self, mode:str):
            # if pilot is controlling both steering and speed
            return self.have_speed_control and (mode == 'local')

    class NotCondition:
        def run(self, condition:bool) -> bool:
            return not condition

    V.add(SpeedControlCondition(have_speed_control), inputs=["user/mode"], outputs=["use_speed_control"])
    V.add(NotCondition(), inputs=["use_speed_control"], outputs=["use_throttle_control"])

    class LedConditionLogic:
        def __init__(self, cfg):
            self.cfg = cfg

        def run(self, mode, recording, recording_alert, behavior_state, model_file_changed, track_loc):
            #returns a blink rate. 0 for off. -1 for on. positive for rate.

            if track_loc is not None:
                led.set_rgb(*self.cfg.LOC_COLORS[track_loc])
                return -1

            if model_file_changed:
                led.set_rgb(self.cfg.MODEL_RELOADED_LED_R, self.cfg.MODEL_RELOADED_LED_G, self.cfg.MODEL_RELOADED_LED_B)
                return 0.1
            else:
                led.set_rgb(self.cfg.LED_R, self.cfg.LED_G, self.cfg.LED_B)

            if recording_alert:
                led.set_rgb(*recording_alert)
                return self.cfg.REC_COUNT_ALERT_BLINK_RATE
            else:
                led.set_rgb(self.cfg.LED_R, self.cfg.LED_G, self.cfg.LED_B)

            if behavior_state is not None and model_type == 'behavior':
                r, g, b = self.cfg.BEHAVIOR_LED_COLORS[behavior_state]
                led.set_rgb(r, g, b)
                return -1 #solid on

            if recording:
                return -1 #solid on
            elif mode == 'user':
                return 1
            elif mode == 'local_angle':
                return 0.5
            elif mode == 'local':
                return 0.1
            return 0

    if cfg.HAVE_RGB_LED and not cfg.DONKEY_GYM:
        from donkeycar.parts.led_status import RGB_LED
        led = RGB_LED(cfg.LED_PIN_R, cfg.LED_PIN_G, cfg.LED_PIN_B, cfg.LED_INVERT)
        led.set_rgb(cfg.LED_R, cfg.LED_G, cfg.LED_B)

        V.add(LedConditionLogic(cfg), inputs=['user/mode', 'recording', "records/alert", 'behavior/state', 'modelfile/modified', "pilot/loc"],
              outputs=['led/blink_rate'])

        V.add(led, inputs=['led/blink_rate'])

    def get_record_alert_color(num_records):
        col = (0, 0, 0)
        for count, color in cfg.RECORD_ALERT_COLOR_ARR:
            if num_records >= count:
                col = color
        return col

    class RecordTracker:
        def __init__(self):
            self.last_num_rec_print = 0
            self.dur_alert = 0
            self.force_alert = 0

        def run(self, num_records):
            if num_records is None:
                return 0

            if self.last_num_rec_print != num_records or self.force_alert:
                self.last_num_rec_print = num_records

                if num_records % 10 == 0:
                    print("recorded", num_records, "records")

                if num_records % cfg.REC_COUNT_ALERT == 0 or self.force_alert:
                    self.dur_alert = num_records // cfg.REC_COUNT_ALERT * cfg.REC_COUNT_ALERT_CYC
                    self.force_alert = 0

            if self.dur_alert > 0:
                self.dur_alert -= 1

            if self.dur_alert != 0:
                return get_record_alert_color(num_records)

            return 0

    rec_tracker_part = RecordTracker()
    V.add(rec_tracker_part, inputs=["tub/num_records"], outputs=['records/alert'])

    if cfg.AUTO_RECORD_ON_THROTTLE:
        def show_record_count_status():
            rec_tracker_part.last_num_rec_print = 0
            rec_tracker_part.force_alert = 1
        if (cfg.CONTROLLER_TYPE != "pigpio_rc") and (cfg.CONTROLLER_TYPE != "MM1"):  # these controllers don't use the joystick class
            if isinstance(ctr, JoystickController):
                ctr.set_button_down_trigger('circle', show_record_count_status) #then we are not using the circle button. hijack that to force a record count indication
        else:
            show_record_count_status()

    #Sombrero
    if cfg.HAVE_SOMBRERO:
        from donkeycar.parts.sombrero import Sombrero
        s = Sombrero()

    #IMU
    if cfg.HAVE_IMU:
        from donkeycar.parts.imu import IMU
        imu = IMU(sensor=cfg.IMU_SENSOR, dlp_setting=cfg.IMU_DLP_CONFIG)
        V.add(imu, outputs=['imu/acl_x', 'imu/acl_y', 'imu/acl_z',
            'imu/gyr_x', 'imu/gyr_y', 'imu/gyr_z'], threaded=True)

    # Use the FPV preview, which will show the cropped image output, or the full frame.
    if cfg.USE_FPV:
        V.add(WebFpv(), inputs=['cam/image_array'], threaded=True)

    def load_model(kl, model_path):
        start = time.time()
        print('loading model', model_path)
        kl.load(model_path)
        print('finished loading in %s sec.' % (str(time.time() - start)) )

    def load_weights(kl, weights_path):
        start = time.time()
        try:
            print('loading model weights', weights_path)
            kl.model.load_weights(weights_path)
            print('finished loading in %s sec.' % (str(time.time() - start)) )
        except Exception as e:
            print(e)
            print('ERR>> problems loading weights', weights_path)

    def load_model_json(kl, json_fnm):
        start = time.time()
        print('loading model json', json_fnm)
        from tensorflow.python import keras
        try:
            with open(json_fnm, 'r') as handle:
                contents = handle.read()
                kl.model = keras.models.model_from_json(contents)
            print('finished loading json in %s sec.' % (str(time.time() - start)) )
        except Exception as e:
            print(e)
            print("ERR>> problems loading model json", json_fnm)

    #
    # load and configure model for inference
    #
    if model_path:
        # create an appropriate Keras part
        kl = dk.utils.get_model_by_type(model_type, cfg)

        #
        # get callback function to reload the model
        # for the configured model format
        #
        model_reload_cb = None
        if '.h5' in model_path or '.trt' in model_path or '.tflite' in \
                model_path or '.savedmodel' in model_path:
            # load the whole model with weigths, etc
            load_model(kl, model_path)

            def reload_model(filename):
                load_model(kl, filename)

            model_reload_cb = reload_model

        elif '.json' in model_path:
            # when we have a .json extension
            # load the model from there and look for a matching
            # .wts file with just weights
            load_model_json(kl, model_path)
            weights_path = model_path.replace('.json', '.weights')
            load_weights(kl, weights_path)

            def reload_weights(filename):
                weights_path = filename.replace('.json', '.weights')
                load_weights(kl, weights_path)

            model_reload_cb = reload_weights

        else:
            print("ERR>> Unknown extension type on model file!!")
            return

        # this part will signal visual LED, if connected
        V.add(FileWatcher(model_path, verbose=True),
              outputs=['modelfile/modified'])

        # these parts will reload the model file, but only when ai is running
        # so we don't interrupt user driving
        V.add(FileWatcher(model_path), outputs=['modelfile/dirty'],
              run_condition="ai_running")
        V.add(DelayedTrigger(100), inputs=['modelfile/dirty'],
              outputs=['modelfile/reload'], run_condition="ai_running")
        V.add(TriggeredCallback(model_path, model_reload_cb),
              inputs=["modelfile/reload"], run_condition="ai_running")

        #
        # collect inputs to model for inference
        #
        if cfg.TRAIN_BEHAVIORS:
            bh = BehaviorPart(cfg.BEHAVIOR_LIST)
            V.add(bh, outputs=['behavior/state', 'behavior/label', "behavior/one_hot_state_array"])
            try:
                ctr.set_button_down_trigger('L1', bh.increment_state)
            except:
                pass

            inputs = ['cam/image_array', "behavior/one_hot_state_array"]

        elif cfg.USE_LIDAR:
            inputs = ['cam/image_array', 'lidar/dist_array']

        elif cfg.HAVE_ODOM:
            inputs = ['cam/image_array', 'enc/speed']

        elif model_type == "imu":
            assert cfg.HAVE_IMU, 'Missing imu parameter in config'
            # Run the pilot if the mode is not user.
            inputs = ['cam/image_array',
                    'imu/acl_x', 'imu/acl_y', 'imu/acl_z',
                    'imu/gyr_x', 'imu/gyr_y', 'imu/gyr_z']
        else:
            inputs = ['cam/image_array']

        #
        # collect model inference outputs
        # - velocity models output normalized forward and angular velocities
        # - other models output normalize throttle and steering values
        #
        if is_velocity_model:
            outputs = ('pilot/norm_angular_velocity', 'pilot/norm_forward_velocity')
        else:
            outputs = ['pilot/angle', 'pilot/throttle']

        if cfg.TRAIN_LOCALIZER:
            outputs.append("pilot/loc")

        #
        # Add image transformations like crop or trapezoidal mask
        #
        if hasattr(cfg, 'TRANSFORMATIONS') and cfg.TRANSFORMATIONS:
            from donkeycar.pipeline.augmentations import ImageAugmentation
            V.add(ImageAugmentation(cfg, 'TRANSFORMATIONS'),
                  inputs=['cam/image_array'], outputs=['cam/image_array_trans'])
            inputs = ['cam/image_array_trans'] + inputs[1:]

        V.add(kl, inputs=inputs, outputs=outputs, run_condition='run_pilot')

        if have_speed_control:
            #
            # pilot outputs normalized velocities.
            # speed control requires actual velocities.
            # so scale normalized value into real range.
            #
            vpart = VelocityUnnormalize(cfg.MIN_SPEED, cfg.MAX_SPEED)
            V.add(vpart, inputs=["pilot/norm_forward_velocity"], outputs=["pilot/speed"], run_condition='use_speed_control')

            # model outputs normalized angular velocity; turn it into unnormalized (real) angular velocity
            if is_differential_drive:
                vpart = UnicycleUnnormalizeAngularVelocity(cfg.WHEEL_RADIUS, cfg.AXLE_LENGTH, cfg.MAX_SPEED)
            else:
                vpart = BicycleUnnormalizeAngularVelocity(cfg.WHEEL_BASE, cfg.MAX_SPEED, cfg.MAX_STEERING_ANGLE)
            V.add(vpart, inputs=["pilot/norm_angular_velocity"], outputs=["pilot/angular_velocity"], run_condition='use_speed_control')


    #
    # stop at a stop sign
    #
    if cfg.STOP_SIGN_DETECTOR:
        from donkeycar.parts.object_detector.stop_sign_detector \
            import StopSignDetector
        V.add(StopSignDetector(cfg.STOP_SIGN_MIN_SCORE,
                               cfg.STOP_SIGN_SHOW_BOUNDING_BOX,
                               cfg.STOP_SIGN_MAX_REVERSE_COUNT,
                               cfg.STOP_SIGN_REVERSE_THROTTLE),
              inputs=['cam/image_array', 'pilot/throttle'],
              outputs=['pilot/throttle', 'cam/image_array'])
        V.add(ThrottleFilter(),
              inputs=['pilot/throttle'],
              outputs=['pilot/throttle'])

    #
    # to give the car a boost when starting ai mode in a race.
    # This will also override the stop sign detector so that
    # you can start at a stop sign using launch mode, but
    # will stop when it comes to the stop sign the next time.
    #
    # NOTE: when launch throttle is in effect, pilot speed is set to None
    #
    aiLauncher = AiLaunch(cfg.AI_LAUNCH_DURATION, cfg.AI_LAUNCH_THROTTLE, cfg.AI_LAUNCH_KEEP_ENABLED)
    V.add(aiLauncher,
          inputs=['user/mode', 'pilot/throttle', 'pilot/speed'],
          outputs=['pilot/throttle', 'pilot/speed'])

    # Choose what inputs should change the car.
    class DriveMode:
        def run(self, mode,
                    user_angle, user_throttle,
                    pilot_angle, pilot_throttle, pilot_speed, pilot_angular_velocity):
            if mode == 'user':
                return user_angle, user_throttle, None, None

            elif mode == 'local_angle':
                return pilot_angle if pilot_angle else 0.0, user_throttle, None, None

            else:
                return pilot_angle if pilot_angle else 0.0, \
                       pilot_throttle * cfg.AI_THROTTLE_MULT if pilot_throttle else 0.0, \
                       pilot_speed, pilot_angular_velocity


    V.add(DriveMode(),
          inputs=['user/mode', 'user/angle', 'user/throttle',
                  'pilot/angle', 'pilot/throttle', 'pilot/speed', 'pilot/angular_velocity'],
          outputs=['angle', 'throttle', 'speed', 'angular_velocity'])

    #
    # Problem: Our 'steering' values of -1 to 1 don't represent actual turn angles or turn rates.
    #          This is fine within a given robot because it's max left and right turn angle
    #          never changes.  However, if you try to share a model with a vehicle that has
    #          different maximum turn angles, then the model will not work. Think about if the
    #          model infers that the turn should be -1; max left turn.  If that model was
    #          learning of a vehicle with a maximum turn angle of 30 degrees, but deployed
    #          on a vehicle with a maximum turn angle of 45 degrees, then you will get very
    #          different turning behavior from the same model.
    #          This is particularly troublesome for a differential drive vehicle trying to
    #          use a model from a car-like (Ackerman steering) vehicle.  Our differential
    #          drive steering algorithm assumes that a steering value of -1, maximum left turn,
    #          stops the left wheel and drives the right wheel at the requested throttle,
    #          so the vehicle pivots around the left wheel in a circle. The actual effective
    #          forward throttle is throttle/2 and the turn is much, much tighter than
    #          would be accomplished in a car-like vehicle.  So our method for differential drive
    #          steering produces very different outcomes than for a car-like vehicle.
    #
    #          I think that is fine in user mode, because the user is the 'controller' and so
    #          the user is generating the forward speed and turn rate that they want.
    #          The real forward velocity and turn rate are captured for differentical drive
    #          vehicles by the Unicycle() part.  We have a similar part for the car-like
    #          vehicles using Bicycle kinematics.  With those we can record real turn rates
    #          in the velocity model.
    #
    #          Once we record real forward velocities and turn rates, then we change the
    #          model to infer these.  Then we will need corresponding reverse kinematics
    #          to take these values and turn them into usable values for the vehicle
    #          running the pilot model.  For car-like vehicles, inverse Bicycle kinematics
    #          produce real forward velocity and real front wheel turn angle.
    #          For differential drive vehicles, inverse Unicycle kinematics produces
    #          real linear velocities for the left and right wheels.
    #
    # TODO: with velocity models, use forward velocity (meters per second) and turn rate (radians per second) normalized,
    #       rather than normalized throttle and steering
    #       so that the velocity models can be interchanged between car-like and differential drive robots, to
    #       the extent the actual vehicle can produce the desired velocities and turning angles.
    #


    #
    # generate final throttle
    # based on model type and drivetrain
    #
    if have_speed_control:
        #
        # We are using a velocity model,
        # so we use speed control to maintain the desired velocity.
        # Add speed controller that takes a speed in meters per second
        # and maintains that speed by modifying the throttle.
        #
        add_speed_control(V, cfg, is_differential_drive)


    #
    # When not using speed control,
    # To make differential drive steer,
    # divide throttle between motors based on the steering value
    #
    if is_differential_drive:
        V.add(TwoWheelSteeringThrottle(),
            inputs=['throttle', 'angle'],
            outputs=['left/throttle', 'right/throttle'],
            run_condition="use_throttle_control")


    if (cfg.CONTROLLER_TYPE != "pigpio_rc") and (cfg.CONTROLLER_TYPE != "MM1"):
        if isinstance(ctr, JoystickController):
            ctr.set_button_down_trigger(cfg.AI_LAUNCH_ENABLE_BUTTON, aiLauncher.enable_ai_launch)

    class AiRunCondition:
        '''
        A bool part to let us know when ai is running.
        '''
        def run(self, mode):
            if mode == "user":
                return False
            return True

    V.add(AiRunCondition(), inputs=['user/mode'], outputs=['ai_running'])

    # Ai Recording
    class AiRecordingCondition:
        '''
        return True when ai mode, otherwize respect user mode recording flag
        '''
        def run(self, mode, recording):
            if mode == 'user':
                return recording
            return True

    if cfg.RECORD_DURING_AI:
        V.add(AiRecordingCondition(), inputs=['user/mode', 'recording'], outputs=['recording'])

    #
    # Setup drivetrain
    #
    add_drivetrain(V, cfg)

    # OLED setup
    if cfg.USE_SSD1306_128_32:
        from donkeycar.parts.oled import OLEDPart
        auto_record_on_throttle = cfg.USE_JOYSTICK_AS_DEFAULT and cfg.AUTO_RECORD_ON_THROTTLE
        oled_part = OLEDPart(cfg.SSD1306_128_32_I2C_ROTATION, cfg.SSD1306_RESOLUTION, auto_record_on_throttle)
        V.add(oled_part, inputs=['recording', 'tub/num_records', 'user/mode'], outputs=[], threaded=True)

    # add tub to save data

    if cfg.USE_LIDAR:
        inputs = ['cam/image_array', 'lidar/dist_array', 'user/angle', 'user/throttle', 'user/mode']
        types = ['image_array', 'nparray','float', 'float', 'str']
    else:
        inputs=['cam/image_array','user/angle', 'user/throttle', 'user/mode']
        types=['image_array','float', 'float','str']

    if cfg.HAVE_ODOM:
        inputs += ['enc/speed']
        types += ['float']

    if cfg.TRAIN_BEHAVIORS:
        inputs += ['behavior/state', 'behavior/label', "behavior/one_hot_state_array"]
        types += ['int', 'str', 'vector']

    if cfg.CAMERA_TYPE == "D435" and cfg.REALSENSE_D435_DEPTH:
        inputs += ['cam/depth_array']
        types += ['gray16_array']

    if cfg.HAVE_IMU or (cfg.CAMERA_TYPE == "D435" and cfg.REALSENSE_D435_IMU):
        inputs += ['imu/acl_x', 'imu/acl_y', 'imu/acl_z',
            'imu/gyr_x', 'imu/gyr_y', 'imu/gyr_z']

        types +=['float', 'float', 'float',
           'float', 'float', 'float']

    # rbx
    if cfg.DONKEY_GYM:
        if cfg.SIM_RECORD_LOCATION:  
            inputs += ['pos/pos_x', 'pos/pos_y', 'pos/pos_z', 'pos/speed', 'pos/cte']
            types  += ['float', 'float', 'float', 'float', 'float']
        if cfg.SIM_RECORD_GYROACCEL: 
            inputs += ['gyro/gyro_x', 'gyro/gyro_y', 'gyro/gyro_z', 'accel/accel_x', 'accel/accel_y', 'accel/accel_z']
            types  += ['float', 'float', 'float', 'float', 'float', 'float']
        if cfg.SIM_RECORD_VELOCITY:  
            inputs += ['vel/vel_x', 'vel/vel_y', 'vel/vel_z']
            types  += ['float', 'float', 'float']
        if cfg.SIM_RECORD_LIDAR:
            inputs += ['lidar/dist_array']
            types  += ['nparray']

    if cfg.RECORD_DURING_AI:
        inputs += ['pilot/angle', 'pilot/throttle']
        types += ['float', 'float']

    if cfg.HAVE_PERFMON:
        from donkeycar.parts.perfmon import PerfMonitor
        mon = PerfMonitor(cfg)
        perfmon_outputs = ['perf/cpu', 'perf/mem', 'perf/freq']
        inputs += perfmon_outputs
        types += ['float', 'float', 'float']
        V.add(mon, inputs=[], outputs=perfmon_outputs, threaded=True)

    # do we want to store new records into own dir or append to existing
    tub_path = TubHandler(path=cfg.DATA_PATH).create_tub_path() if \
        cfg.AUTO_CREATE_NEW_TUB else cfg.DATA_PATH
    tub_writer = TubWriter(tub_path, inputs=inputs, types=types, metadata=meta)
    V.add(tub_writer, inputs=inputs, outputs=["tub/num_records"], run_condition='recording')

    # Telemetry (we add the same metrics added to the TubHandler
    if cfg.HAVE_MQTT_TELEMETRY:
        from donkeycar.parts.telemetry import MqttTelemetry
        tel = MqttTelemetry(cfg)
        telem_inputs, _ = tel.add_step_inputs(inputs, types)
        V.add(tel, inputs=telem_inputs, outputs=["tub/queue_size"], threaded=True)

    if cfg.PUB_CAMERA_IMAGES:
        from donkeycar.parts.network import TCPServeValue
        from donkeycar.parts.image import ImgArrToJpg
        pub = TCPServeValue("camera")
        V.add(ImgArrToJpg(), inputs=['cam/image_array'], outputs=['jpg/bin'])
        V.add(pub, inputs=['jpg/bin'])


    if cfg.DONKEY_GYM:
        print("You can now go to http://localhost:%d to drive your car." % cfg.WEB_CONTROL_PORT)
    else:
        print("You can now go to <your hostname.local>:%d to drive your car." % cfg.WEB_CONTROL_PORT)
    if has_input_controller:
        print("You can now move your controller to drive your car.")
        if isinstance(ctr, JoystickController):
            ctr.set_tub(tub_writer.tub)
            ctr.print_controls()

    # run the vehicle
    V.start(rate_hz=cfg.DRIVE_LOOP_HZ, max_loop_count=cfg.MAX_LOOPS)


def get_user_controller(cfg):
    """
    Get controller for user input.
    The controller gets a camera image as input (in case
    it is a remote controller and it shows that feed to the user)
    The controller must output
    - the steering value,
    - the throttle value,
    - the user mode (true or false) and
    - the recording mode (true or false).
    The controller must be thread enabled.
    """
    #modify max_throttle closer to 1.0 to have more power
    #modify steering_scale lower than 1.0 to have less responsive steering
    ctr = None
    if hasattr(cfg, "CONTROLLER_TYPE") and cfg.CONTROLLER_TYPE != "mock":
        if cfg.CONTROLLER_TYPE == "pigpio_rc":    # an RC controllers read by GPIO pins. They typically don't have buttons
            from donkeycar.parts.controller import RCReceiver
            ctr = RCReceiver(cfg)
            V.add(ctr, outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],threaded=False)
        elif cfg.CONTROLLER_TYPE == "custom":  #custom controller created with `donkey createjs` command
            from my_joystick import MyJoystickController
            ctr = MyJoystickController(
            throttle_dir=cfg.JOYSTICK_THROTTLE_DIR,
            throttle_scale=cfg.JOYSTICK_MAX_THROTTLE,
            steering_scale=cfg.JOYSTICK_STEERING_SCALE,
            auto_record_on_throttle=cfg.AUTO_RECORD_ON_THROTTLE)
            ctr.set_deadzone(cfg.JOYSTICK_DEADZONE)
        elif cfg.CONTROLLER_TYPE == "MM1":
            from donkeycar.parts.robohat import RoboHATController
            ctr = RoboHATController(cfg)
        else:
            from donkeycar.parts.controller import get_js_controller
            ctr = get_js_controller(cfg)
            if ctr:
                if cfg.USE_NETWORKED_JS:
                    from donkeycar.parts.controller import JoyStickSub
                    netwkJs = JoyStickSub(cfg.NETWORK_JS_SERVER_IP)
                    V.add(netwkJs, threaded=True)
                    ctr.js = netwkJs
            else:
                raise ValueError(f"Unknown CONTROLLER_TYPE ({cfg.CONTROLLER_TYPE})")
    if ctr:
        run_threaded = getattr(ctr, "run_threaded", None)
        if run_threaded is None or not callable(run_threaded):
            raise TypeError("The controller must support run_threaded()")
    else:
        logger.info("No user input controller configured")
    return ctr


def add_user_controller(V, cfg, use_joystick):
    """
    Add the web controller and any other
    configured user input controller.
    :param V: the vehicle pipeline.
              On output this will be modified.
    :param cfg: the configuration (from myconfig.py)
    :return: the controller
    """
    # This web controller will create a web server that is capable
    # of managing steering, throttle, and modes, and more.
    # it will also show the camera feed
    ctr = LocalWebController(port=cfg.WEB_CONTROL_PORT, mode=cfg.WEB_INIT_MODE)
    V.add(ctr,
        inputs=['cam/image_array', 'tub/num_records'],
        outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],
        threaded=True)

    if use_joystick or cfg.USE_JOYSTICK_AS_DEFAULT:
        ctr = get_user_controller(cfg)
        if ctr:
            V.add(ctr, inputs=['cam/image_array'], outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],threaded=True)
    return ctr


def get_camera(cfg):
    """
    Get the configured camera part
    """
    if cfg.DONKEY_GYM:
        from donkeycar.parts.dgym import DonkeyGymEnv
        #rbx
        cam = DonkeyGymEnv(cfg.DONKEY_SIM_PATH, host=cfg.SIM_HOST, env_name=cfg.DONKEY_GYM_ENV_NAME, conf=cfg.GYM_CONF, record_location=cfg.SIM_RECORD_LOCATION, record_gyroaccel=cfg.SIM_RECORD_GYROACCEL, record_velocity=cfg.SIM_RECORD_VELOCITY, record_lidar=cfg.SIM_RECORD_LIDAR, delay=cfg.SIM_ARTIFICIAL_LATENCY)
        threaded = True
        inputs = ['angle', 'throttle']
    elif cfg.CAMERA_TYPE == "PICAM":
        from donkeycar.parts.camera import PiCamera
        cam = PiCamera(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH,
                       framerate=cfg.CAMERA_FRAMERATE,
                       vflip=cfg.CAMERA_VFLIP, hflip=cfg.CAMERA_HFLIP)
    elif cfg.CAMERA_TYPE == "WEBCAM":
        from donkeycar.parts.camera import Webcam
        cam = Webcam(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH)
    elif cfg.CAMERA_TYPE == "CVCAM":
        from donkeycar.parts.cv import CvCam
        cam = CvCam(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH)
    elif cfg.CAMERA_TYPE == "CSIC":
        from donkeycar.parts.camera import CSICamera
        cam = CSICamera(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH,
                        capture_width=cfg.IMAGE_W, capture_height=cfg.IMAGE_H,
                        framerate=cfg.CAMERA_FRAMERATE, gstreamer_flip=cfg.CSIC_CAM_GSTREAMER_FLIP_PARM)
    elif cfg.CAMERA_TYPE == "V4L":
        from donkeycar.parts.camera import V4LCamera
        cam = V4LCamera(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH, framerate=cfg.CAMERA_FRAMERATE)
    elif cfg.CAMERA_TYPE == "MOCK":
        from donkeycar.parts.camera import MockCamera
        cam = MockCamera(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH)
    elif cfg.CAMERA_TYPE == "IMAGE_LIST":
        from donkeycar.parts.camera import ImageListCamera
        cam = ImageListCamera(path_mask=cfg.PATH_MASK)
    elif cfg.CAMERA_TYPE == "LEOPARD":
        from donkeycar.parts.leopard_imaging import LICamera
        cam = LICamera(width=cfg.IMAGE_W, height=cfg.IMAGE_H, fps=cfg.CAMERA_FRAMERATE)
    else:
        raise(Exception("Unkown camera type: %s" % cfg.CAMERA_TYPE))
    return cam


def add_camera(V, cfg, camera_type):
    """
    Add the configured camera to the vehicle pipeline.

    :param V: the vehicle pipeline.
              On output this will be modified.
    :param cfg: the configuration (from myconfig.py)
    """
    logger.info("cfg.CAMERA_TYPE %s"%cfg.CAMERA_TYPE)
    if camera_type == "stereo":

        if cfg.CAMERA_TYPE == "WEBCAM":
            from donkeycar.parts.camera import Webcam

            camA = Webcam(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH, iCam = 0)
            camB = Webcam(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH, iCam = 1)

        elif cfg.CAMERA_TYPE == "CVCAM":
            from donkeycar.parts.cv import CvCam

            camA = CvCam(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH, iCam = 0)
            camB = CvCam(image_w=cfg.IMAGE_W, image_h=cfg.IMAGE_H, image_d=cfg.IMAGE_DEPTH, iCam = 1)
        else:
            raise(Exception("Unsupported camera type: %s" % cfg.CAMERA_TYPE))

        V.add(camA, outputs=['cam/image_array_a'], threaded=True)
        V.add(camB, outputs=['cam/image_array_b'], threaded=True)

        from donkeycar.parts.image import StereoPair

        V.add(StereoPair(), inputs=['cam/image_array_a', 'cam/image_array_b'],
            outputs=['cam/image_array'])
    elif cfg.CAMERA_TYPE == "D435":
        from donkeycar.parts.realsense435i import RealSense435i
        cam = RealSense435i(
            enable_rgb=cfg.REALSENSE_D435_RGB,
            enable_depth=cfg.REALSENSE_D435_DEPTH,
            enable_imu=cfg.REALSENSE_D435_IMU,
            device_id=cfg.REALSENSE_D435_ID)
        V.add(cam, inputs=[],
              outputs=['cam/image_array', 'cam/depth_array',
                       'imu/acl_x', 'imu/acl_y', 'imu/acl_z',
                       'imu/gyr_x', 'imu/gyr_y', 'imu/gyr_z'],
              threaded=True)

    else:
        inputs = []
        outputs = ['cam/image_array']
        threaded = True
        cam = get_camera(cfg)

        # Donkey gym part will output position information if it is configured
        # TODO: the simulation outputs conflict with imu, odometry, kinematics pose estimation and T265 outputs; make them work together.
        if cfg.DONKEY_GYM:
            if cfg.SIM_RECORD_LOCATION:
                outputs += ['pos/pos_x', 'pos/pos_y', 'pos/pos_z', 'pos/speed', 'pos/cte']
            if cfg.SIM_RECORD_GYROACCEL:
                outputs += ['gyro/gyro_x', 'gyro/gyro_y', 'gyro/gyro_z', 'accel/accel_x', 'accel/accel_y', 'accel/accel_z']
            if cfg.SIM_RECORD_VELOCITY:
                outputs += ['vel/vel_x', 'vel/vel_y', 'vel/vel_z']
            if cfg.SIM_RECORD_LIDAR:
                outputs += ['lidar/dist_array']

        V.add(cam, inputs=inputs, outputs=outputs, threaded=threaded)


def add_odometry(V, cfg):
    """
    If the configuration support odometry, then
    add encoders, odometry and kinematics to the vehicle pipeline
    :param V: the vehicle pipeline.
              On output this may be modified.
    :param cfg: the configuration (from myconfig.py)
    """
    if cfg.HAVE_ODOM:
        from donkeycar.utilities.serial_port import SerialPort
        from donkeycar.parts.tachometer import (Tachometer, SerialEncoder, GpioEncoder, EncoderChannel)
        from donkeycar.parts.odometer import Odometer
        from donkeycar.parts import pins;

        tachometer = None
        tachometer2 = None
        if cfg.ENCODER_TYPE == "GPIO":
            tachometer = Tachometer(
                GpioEncoder(gpio_pin=pins.input_pin_by_id(cfg.ODOM_PIN),
                            debounce_ns=cfg.ENCODER_DEBOUNCE_NS,
                            debug=cfg.ODOM_DEBUG),
                ticks_per_revolution=cfg.ENCODER_PPR,
                direction_mode=cfg.TACHOMETER_MODE,
                poll_delay_secs=1.0/(cfg.DRIVE_LOOP_HZ*3),
                debug=cfg.ODOM_DEBUG)
            if cfg.HAVE_ODOM_2:
                tachometer2 = Tachometer(
                    GpioEncoder(gpio_pin=pins.input_pin_by_id(cfg.ODOM_PIN_2),
                                debounce_ns=cfg.ENCODER_DEBOUNCE_NS,
                                debug=cfg.ODOM_DEBUG),
                    ticks_per_revolution=cfg.ENCODER_PPR,
                    direction_mode=cfg.TACHOMETER_MODE,
                    poll_delay_secs=1.0/(cfg.DRIVE_LOOP_HZ*3),
                    debug=cfg.ODOM_DEBUG)

        elif cfg.ENCODER_TYPE == "arduino":
            tachometer = Tachometer(
                SerialEncoder(serial_port=SerialPort(cfg.ODOM_SERIAL, cfg.ODOM_SERIAL_BAUDRATE),debug=cfg.ODOM_DEBUG),
                ticks_per_revolution=cfg.ENCODER_PPR,
                direction_mode=cfg.TACHOMETER_MODE,
                poll_delay_secs=1.0/(cfg.DRIVE_LOOP_HZ*3),
                debug=cfg.ODOM_DEBUG)
            if cfg.HAVE_ODOM_2:
                tachometer2 = Tachometer(
                    EncoderChannel(encoder=tachometer.encoder, channel=1),
                    ticks_per_revolution=cfg.ENCODER_PPR,
                    direction_mode=cfg.TACHOMETER_MODE,
                    poll_delay_secs=1.0/(cfg.DRIVE_LOOP_HZ*3),
                    debug=cfg.ODOM_DEBUG)

        else:
            print("No supported encoder found")

        if tachometer:
            if cfg.HAVE_ODOM_2:
                #
                # A second odometer is configured; assume a
                # differential drivetrain.  Use Unicycle
                # kinematics to synthesize a single distance
                # and velocity from the two wheels.
                #
                odometer1 = Odometer(
                    distance_per_revolution=cfg.ENCODER_PPR * cfg.MM_PER_TICK / 1000,
                    smoothing_count=cfg.ODOM_SMOOTHING,
                    debug=cfg.ODOM_DEBUG)
                odometer2 = Odometer(
                    distance_per_revolution=cfg.ENCODER_PPR * cfg.MM_PER_TICK / 1000,
                    smoothing_count=cfg.ODOM_SMOOTHING,
                    debug=cfg.ODOM_DEBUG)
                V.add(tachometer, inputs=['throttle', None], outputs=['enc/left/revolutions', 'enc/left/timestamp'], threaded=True)
                V.add(
                    odometer1,
                    inputs=['enc/left/revolutions', 'enc/left/timestamp'],
                    outputs=['enc/left/distance', 'enc/left/speed', 'enc/left/timestamp'],
                    threaded=False)
                V.add(tachometer2, inputs=['throttle', None], outputs=['enc/right/revolutions', 'enc/right/timestamp'], threaded=True)
                V.add(odometer2, inputs=['enc/right/revolutions', 'enc/right/timestamp'], outputs=['enc/right/distance', 'enc/right/speed', 'enc/right/timestamp'], threaded=False)
                V.add(
                    Unicycle(cfg.AXLE_LENGTH, cfg.ODOM_DEBUG),
                    inputs=['enc/left/distance', 'enc/right/distance', 'enc/left/timestamp'],
                    outputs=['enc/distance', 'enc/speed', 'pos/x', 'pos/y', 'pos/angle', 'vel/x', 'vel/y', 'vel/angle', 'nul/timestamp'],
                    threaded=False)

            else:
                # single odometer directly measures distance and velocity
                odometer = Odometer(
                    distance_per_revolution=cfg.ENCODER_PPR * cfg.MM_PER_TICK / 1000,
                    smoothing_count=cfg.ODOM_SMOOTHING,
                    debug=cfg.ODOM_DEBUG)
                V.add(tachometer, inputs=['throttle', None], outputs=['enc/revolutions', 'enc/timestamp'], threaded=True)
                V.add(odometer, inputs=['enc/revolutions', 'enc/timestamp'], outputs=['enc/distance', 'enc/speed', 'enc/timestamp'], threaded=False)
                V.add(UnnormalizeSteeringAngle(cfg.MAX_STEERING_ANGLE),
                      inputs=["steering"], outputs=["steering_angle"])
                V.add(
                    Bicycle(cfg.WHEEL_BASE, cfg.MAX_STEERING_ANGLE),
                    inputs=["enc/distance", "steering_angle", "enc/timestamp"],
                    outputs=["nul/distance, nul/speed", 'pos/x', 'pos/y', 'pos/angle', 'vel/x', 'vel/y', 'vel/angle', 'nul/timestamp'])


def add_speed_control(V, cfg, is_differential_drive):
    """
    Add a speed controller to maintain a desired velocity.
    The speed controller that takes a speed in meters per second
    and maintains that speed by modifying the throttle.
    :param V: the vehicle pipeline.
              On output this may be modified.
    :param cfg: the configuration (from myconfig.py)    """
    # TODO: This uses a simple step controller: make speed controller pluggable/configurable.
    if is_differential_drive:
        #
        # Use inverse kinematics to convert steering angle and speed into
        # individual wheel speeds.
        #
        kinematics = InverseUnicycle(cfg.AXLE_LENGTH, cfg.WHEEL_RADIUS, cfg.MIN_SPEED, cfg.MAX_SPEED)
        V.add(kinematics,
            inputs=["speed", "angular_velocity", "enc/timestamp"],
            outputs=["left/speed", "right/speed", "nul"],
            run_condition="use_speed_control")

        #
        # Add a speed controller to each wheel to maintain the speed and turn angle.
        # The speed controller takes measured speed and desired speed and modifies
        # the throttle to achieve the desired speed.
        #
        speed_controller = StepSpeedController(cfg.MIN_SPEED, cfg.MAX_SPEED, (1.0 - cfg.MIN_THROTTLE) / 255, cfg.MIN_THROTTLE)
        V.add(speed_controller,
            inputs=["left/throttle", "enc/left/speed", "left/speed"],
            outputs=["left/throttle"],
            run_condition="use_speed_control")
        speed_controller = StepSpeedController(cfg.MIN_SPEED, cfg.MAX_SPEED, (1.0 - cfg.MIN_THROTTLE) / 255, cfg.MIN_THROTTLE)
        V.add(speed_controller,
            inputs=["left/throttle", "enc/right/speed", "right/speed"],
            outputs=["right/throttle"],
            run_condition="use_speed_control")

    else: # car-type vehicle
        #
        # use bicycle inverse kinematics to get steering angle
        #
        kinematics = InverseBicycle(cfg.WHEEL_BASE)
        V.add(kinematics,
            inputs=["speed", "angular_velocity", "enc/timestamp"],
            outputs=["speed", "steering_angle", "nul"],
            run_condition="use_speed_control")

        # convert steering angle to normalized value that drivetrains expect
        V.add(NormalizeSteeringAngle(cfg.MAX_STEERING_ANGLE, cfg.STEERING_ZERO),
            inputs=["steering_angle"], outputs=["angle"], run_condition="use_speed_control")

        # add a speed controller to maintain the desired speed
        speed_controller = StepSpeedController(cfg.MIN_SPEED, cfg.MAX_SPEED, (1.0 - cfg.MIN_THROTTLE) / 255, cfg.MIN_THROTTLE)
        V.add(speed_controller,
            inputs=["throttle", "enc/speed", "speed"],
            outputs=["throttle"],
            run_condition="use_speed_control")


#
# Drive train setup
#
def add_drivetrain(V, cfg):
    from donkeycar.parts import actuator, pins;

    if (not cfg.DONKEY_GYM) and cfg.DRIVE_TRAIN_TYPE != "MOCK":
        if cfg.DRIVE_TRAIN_TYPE == "PWM_STEERING_THROTTLE":
            #
            # drivetrain for RC car with servo and ESC.
            # using a PwmPin for steering (servo)
            # and as second PwmPin for throttle (ESC)
            #
            from donkeycar.parts.actuator import PWMSteering, PWMThrottle, PulseController
            steering_controller = PulseController(
                pwm_pin=pins.pwm_pin_by_id(cfg.PWM_STEERING_PIN),
                pwm_scale=cfg.PWM_STEERING_SCALE, 
                pwm_inverted=cfg.PWM_STEERING_INVERTED)
            steering = PWMSteering(controller=steering_controller,
                                            left_pulse=cfg.STEERING_LEFT_PWM, 
                                            right_pulse=cfg.STEERING_RIGHT_PWM)
            
            throttle_controller = PulseController(
                pwm_pin=pins.pwm_pin_by_id(cfg.PWM_THROTTLE_PIN), 
                pwm_scale=cfg.PWM_THROTTLE_SCALE, 
                pwm_inverted=cfg.PWM_THROTTLE_INVERTED)
            throttle = PWMThrottle(controller=throttle_controller,
                                                max_pulse=cfg.THROTTLE_FORWARD_PWM,
                                                zero_pulse=cfg.THROTTLE_STOPPED_PWM, 
                                                min_pulse=cfg.THROTTLE_REVERSE_PWM)
            V.add(steering, inputs=['angle'], threaded=True)
            V.add(throttle, inputs=['throttle'], threaded=True)

        elif cfg.DRIVE_TRAIN_TYPE == "I2C_SERVO":
            #
            # Thi driver is DEPRECATED in favor of 'DRIVE_TRAIN_TYPE == "PWM_STEERING_THROTTLE"'
            # This driver will be removed in a future release
            #
            from donkeycar.parts.actuator import PCA9685, PWMSteering, PWMThrottle

            steering_controller = PCA9685(cfg.STEERING_CHANNEL, cfg.PCA9685_I2C_ADDR, busnum=cfg.PCA9685_I2C_BUSNUM)
            steering = PWMSteering(controller=steering_controller,
                                            left_pulse=cfg.STEERING_LEFT_PWM,
                                            right_pulse=cfg.STEERING_RIGHT_PWM)

            throttle_controller = PCA9685(cfg.THROTTLE_CHANNEL, cfg.PCA9685_I2C_ADDR, busnum=cfg.PCA9685_I2C_BUSNUM)
            throttle = PWMThrottle(controller=throttle_controller,
                                            max_pulse=cfg.THROTTLE_FORWARD_PWM,
                                            zero_pulse=cfg.THROTTLE_STOPPED_PWM,
                                            min_pulse=cfg.THROTTLE_REVERSE_PWM)

            V.add(steering, inputs=['angle'], threaded=True)
            V.add(throttle, inputs=['throttle'], threaded=True)

        elif cfg.DRIVE_TRAIN_TYPE == "DC_STEER_THROTTLE":
            steering = actuator.L298N_HBridge_2pin(
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_LEFT), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_RIGHT))
            throttle = Mini_HBridge_DC_Motor_PWM(
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_FWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_BWD))

            V.add(steering, inputs=['angle'])
            V.add(throttle, inputs=['throttle'])

        elif cfg.DRIVE_TRAIN_TYPE == "DC_TWO_WHEEL":
            left_motor = actuator.L298N_HBridge_2pin(
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_LEFT_FWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_LEFT_BWD))
            right_motor = actuator.L298N_HBridge_2pin(
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_RIGHT_FWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_PIN_RIGHT_BWD))

            V.add(left_motor, inputs=['left/throttle'])
            V.add(right_motor, inputs=['right/throttle'])

        elif cfg.DRIVE_TRAIN_TYPE == "DC_TWO_WHEEL_L298N":
            left_motor = actuator.L298N_HBridge_3pin(
                pins.output_pin_by_id(cfg.HBRIDGE_L298N_PIN_LEFT_FWD), 
                pins.output_pin_by_id(cfg.HBRIDGE_L298N_PIN_LEFT_BWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_L298N_PIN_LEFT_EN))
            right_motor = actuator.L298N_HBridge_3pin(
                pins.output_pin_by_id(cfg.HBRIDGE_L298N_PIN_RIGHT_FWD), 
                pins.output_pin_by_id(cfg.HBRIDGE_L298N_PIN_RIGHT_BWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_L298N_PIN_RIGHT_EN))

            V.add(left_motor, inputs=['left/throttle'])
            V.add(right_motor, inputs=['right/throttle'])

        elif cfg.DRIVE_TRAIN_TYPE == "SERVO_HBRIDGE_2PIN":
            #
            # Servo for steering and HBridge motor driver in 2pin mode for motor
            #
            from donkeycar.parts.actuator import PWMSteering, PWMThrottle, PulseController
            steering_controller = PulseController(
                pwm_pin=pins.pwm_pin_by_id(cfg.PWM_STEERING_PIN),
                pwm_scale=cfg.PWM_STEERING_SCALE, 
                pwm_inverted=cfg.PWM_STEERING_INVERTED)
            steering = PWMSteering(controller=steering_controller,
                                            left_pulse=cfg.STEERING_LEFT_PWM, 
                                            right_pulse=cfg.STEERING_RIGHT_PWM)

            motor = actuator.L298N_HBridge_2pin(
                pins.pwm_pin_by_id(cfg.HBRIDGE_2PIN_DUTY_FWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_2PIN_DUTY_BWD))

            V.add(steering, inputs=['angle'], threaded=True)
            V.add(motor, inputs=["throttle"])
            
        elif cfg.DRIVE_TRAIN_TYPE == "SERVO_HBRIDGE_3PIN":
            #
            # Servo for steering and HBridge motor driver in 3pin mode for motor
            #
            from donkeycar.parts.actuator import PWMSteering, PWMThrottle, PulseController
            steering_controller = PulseController(
                pwm_pin=pins.pwm_pin_by_id(cfg.PWM_STEERING_PIN),
                pwm_scale=cfg.PWM_STEERING_SCALE, 
                pwm_inverted=cfg.PWM_STEERING_INVERTED)
            steering = PWMSteering(controller=steering_controller,
                                            left_pulse=cfg.STEERING_LEFT_PWM, 
                                            right_pulse=cfg.STEERING_RIGHT_PWM)

            motor = actuator.L298N_HBridge_3pin(
                pins.output_pin_by_id(cfg.HBRIDGE_3PIN_FWD), 
                pins.output_pin_by_id(cfg.HBRIDGE_3PIN_BWD), 
                pins.pwm_pin_by_id(cfg.HBRIDGE_3PIN_DUTY))

            V.add(steering, inputs=['angle'], threaded=True)
            V.add(motor, inputs=["throttle"])
            
        elif cfg.DRIVE_TRAIN_TYPE == "SERVO_HBRIDGE_PWM":
            #
            # Thi driver is DEPRECATED in favor of 'DRIVE_TRAIN_TYPE == "SERVO_HBRIDGE_2PIN"'
            # This driver will be removed in a future release
            #
            from donkeycar.parts.actuator import ServoBlaster, PWMSteering
            steering_controller = ServoBlaster(cfg.STEERING_CHANNEL) #really pin
            # PWM pulse values should be in the range of 100 to 200
            assert(cfg.STEERING_LEFT_PWM <= 200)
            assert(cfg.STEERING_RIGHT_PWM <= 200)
            steering = PWMSteering(controller=steering_controller,
                                   left_pulse=cfg.STEERING_LEFT_PWM,
                                   right_pulse=cfg.STEERING_RIGHT_PWM)

            from donkeycar.parts.actuator import Mini_HBridge_DC_Motor_PWM
            motor = Mini_HBridge_DC_Motor_PWM(cfg.HBRIDGE_PIN_FWD, cfg.HBRIDGE_PIN_BWD)

            V.add(steering, inputs=['angle'], threaded=True)
            V.add(motor, inputs=["throttle"])
            
        elif cfg.DRIVE_TRAIN_TYPE == "MM1":
            from donkeycar.parts.robohat import RoboHATDriver
            V.add(RoboHATDriver(cfg), inputs=['angle', 'throttle'])
        
        elif cfg.DRIVE_TRAIN_TYPE == "PIGPIO_PWM":
            #
            # Thi driver is DEPRECATED in favor of 'DRIVE_TRAIN_TYPE == "PWM_STEERING_THROTTLE"'
            # This driver will be removed in a future release
            #
            from donkeycar.parts.actuator import PWMSteering, PWMThrottle, PiGPIO_PWM
            steering_controller = PiGPIO_PWM(cfg.STEERING_PWM_PIN, freq=cfg.STEERING_PWM_FREQ, inverted=cfg.STEERING_PWM_INVERTED)
            steering = PWMSteering(controller=steering_controller,
                                            left_pulse=cfg.STEERING_LEFT_PWM, 
                                            right_pulse=cfg.STEERING_RIGHT_PWM)
            
            throttle_controller = PiGPIO_PWM(cfg.THROTTLE_PWM_PIN, freq=cfg.THROTTLE_PWM_FREQ, inverted=cfg.THROTTLE_PWM_INVERTED)
            throttle = PWMThrottle(controller=throttle_controller,
                                                max_pulse=cfg.THROTTLE_FORWARD_PWM,
                                                zero_pulse=cfg.THROTTLE_STOPPED_PWM, 
                                                min_pulse=cfg.THROTTLE_REVERSE_PWM)
            V.add(steering, inputs=['angle'], threaded=True)
            V.add(throttle, inputs=['throttle'], threaded=True)

if __name__ == '__main__':
    args = docopt(__doc__)
    cfg = dk.load_config(myconfig=args['--myconfig'])

    if args['drive']:
        model_type = args['--type']
        camera_type = args['--camera']
        drive(cfg, model_path=args['--model'], use_joystick=args['--js'],
              model_type=model_type, camera_type=camera_type,
              meta=args['--meta'])
    elif args['train']:
        print('Use python train.py instead.\n')
