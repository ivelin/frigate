import datetime
import time
import cv2
from PIL import Image
import threading
import numpy as np
from edgetpu.detection.engine import DetectionEngine
from . util import tonumpyarray
import os
import json

# Default path to frozen detection graph. This is the actual model that is used for the object detection.
PATH_TO_CKPT = '/frozen_inference_graph.pb'
# Default path to list of the strings that is used to add correct label for each box.
PATH_TO_LABELS = '/label_map.pbtext'
# Default data_dir used to save detections for later use such as re-labeling and training
DATA_DIR = '/data/'


# Function to read labels from text files.
def ReadLabelFile(file_path):
    print("Loading Tensorflow model labels: " + str(file_path))
    with open(file_path, 'r') as f:
        lines = f.readlines()
    ret = {}
    for line in lines:
        pair = line.strip().split(maxsplit=1)
        ret[int(pair[0])] = pair[1].strip()
    return ret

class PreppedQueueProcessor(threading.Thread):
    def __init__(self, cameras, prepped_frame_queue, tf_args):

        threading.Thread.__init__(self)
        self.cameras = cameras
        self.prepped_frame_queue = prepped_frame_queue
        
        # print("tf_args: " + str(tf_args))
        # Load the edgetpu engine and labels
        if tf_args != None:
            tf_model = tf_args.get('model', None)
            tf_labels = tf_args.get('labels', None)
            self.data_dir = tf_args.get('data_dir', DATA_DIR)
        else:
            tf_model = PATH_TO_CKPT
            tf_labels = PATH_TO_LABELS
            self.data_dir = DATA_DIR
        print("Loading Tensorflow model: " + str(tf_model))
        self.engine = DetectionEngine(tf_model)
        if tf_labels:
            self.labels = ReadLabelFile(tf_labels)
        else:
            self.labels = None
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        self.last_saved_det_frame_time = yesterday
        self.last_saved_non_det_frame_time = yesterday

    def run(self):
        # process queue...
        while True:
            frame = self.prepped_frame_queue.get()

            # Actual detection.
#            objects = self.engine.DetectWithInputTensor(frame['frame'], threshold=frame['region_threshold'], top_k=3)
            # print(self.engine.get_inference_time())

            try:
                # print("Calling TF Coral to detect objects")
                detected_objects = self.engine.DetectWithImage(
                    frame['img'],
                    threshold=frame['region_threshold'],
                    keep_aspect_ratio=True,
                    relative_coord=True,
                    top_k=3)
                #print("TF Coral detected {} objects withing {} ms".
                #      format(len(objects),
                #             self.engine.get_inference_time()))

                # parse and pass detected objects back to the outbound camera stream
                self.__pass_detections_to_output_cam(frame, detected_objects)

                # save detections to file if enabled
                self.__save_sample(frame, detected_objects)
            except Exception as e:
                print("Error while calling TF Coral: \n" + str(e))

    # Parse and pass detected objects back to the outbound camera stream
    def __pass_detections_to_output_cam(self, frame, detected_objects):
        parsed_objects = []
        for obj in detected_objects:
            # print("Detected an object: \n\n")
            # print("Detected object label index: " + str(obj.label_id) + "\n\n")
            box = obj.bounding_box.flatten().tolist()
            if self.labels:
                obj_name = str(self.labels[obj.label_id])
            else:
                obj_name = " "  # no labels, just a yes/no type of detection
            detection = {
                'frame_time': frame['frame_time'],
                'name': obj_name,
                'score': float(obj.score),
                'xmin': int((box[0] * frame['region_size']) + frame['region_x_offset']),
                'ymin': int((box[1] * frame['region_size']) + frame['region_y_offset']),
                'xmax': int((box[2] * frame['region_size']) + frame['region_x_offset']),
                'ymax': int((box[3] * frame['region_size']) + frame['region_y_offset'])
            }
            # print(str(detection) + "\n")
            parsed_objects.append(detection)
        self.cameras[frame['camera_name']].add_objects(parsed_objects)

    # Save detections to file for offline processing
    def __save_sample(self, frame, detected_objects):
        try:
            if frame['save_samples']:
                now = datetime.datetime.now()
                dir = self.data_dir + frame['camera_name']
                os.makedirs(dir, exist_ok=True)
                # pretty up the timestamp
                file_prefix = now.strftime("%Y%m%d-%H%M%S")
                if detected_objects:
                    # default 2 seconds between saved detection samples
                    save_det_interval = frame['save_samples'].get('detections_interval', 2)
                    # print("Interval for saved detection samples : {}".format(save_det_interval))
                    # check if enough time has passed since the last saved detection sample
                    if (now - self.last_saved_det_frame_time).total_seconds() >= save_det_interval:
                        dir += "/detections/"
                        os.makedirs(dir, exist_ok=True)
                        det_file_path = dir + file_prefix + ".json"
                        detection_json = []
                        for obj in detected_objects:
                            box = obj.bounding_box.flatten().tolist()
                            if self.labels:
                                obj_name = str(self.labels[obj.label_id])
                            else:
                                obj_name = " "  # no labels, just a yes/no type of detection
                            next_detection = {
                                'time': now.isoformat(' '),
                                'label_id': obj.label_id,
                                'name': obj_name,
                                'score': float(obj.score),
                                'xmin': int((box[0] * frame['region_size'])),
                                'ymin': int((box[1] * frame['region_size'])),
                                'xmax': int((box[2] * frame['region_size'])),
                                'ymax': int((box[3] * frame['region_size']))
                            }
                            detection_json.append(next_detection)
                        with open(det_file_path, 'w', encoding='utf-8') as f:
                            json.dump(detection_json, f, ensure_ascii=False, indent=4)

                        self.last_saved_det_frame_time = now
                        img_file_path = dir + file_prefix + ".jpg"
                        img = frame['img']
                        img.save(img_file_path, "JPEG")
                else:
                    # default 300 seconds (5 min) between saved non detection samples
                    save_non_det_interval = frame['save_samples'].get('non_detections_interval', 300)
                    # print("Interval for saved non-detection samples : {}".format(save_non_det_interval))
                    # check if enough time has passed since the last saved non-detection sample
                    if (now - self.last_saved_non_det_frame_time).total_seconds() >= save_non_det_interval:
                        dir += "/non_detections/"
                        os.makedirs(dir, exist_ok=True)
                        self.last_saved_non_det_frame_time = now
                        img_file_path = dir + file_prefix + ".jpg"
                        img = frame['img']
                        img.save(img_file_path, "JPEG")
        except Exception as e:
            print("Error while saving an image region sample: \n" + str(e))


# should this be a region class?
class FramePrepper(threading.Thread):
    def __init__(self, camera_name, shared_frame, frame_time, frame_ready, 
        frame_lock,
        region_size, region_x_offset, region_y_offset, region_threshold,
        region_save_samples,
        prepped_frame_queue):

        threading.Thread.__init__(self)
        self.camera_name = camera_name
        self.shared_frame = shared_frame
        self.frame_time = frame_time
        self.frame_ready = frame_ready
        self.frame_lock = frame_lock
        self.region_size = region_size
        self.region_x_offset = region_x_offset
        self.region_y_offset = region_y_offset
        self.region_threshold = region_threshold
        self.region_save_samples = region_save_samples
        self.prepped_frame_queue = prepped_frame_queue

    def run(self):
        frame_time = 0.0
        while True:
            now = datetime.datetime.now().timestamp()

            with self.frame_ready:
                # if there isnt a frame ready for processing or it is old, wait for a new frame
                if self.frame_time.value == frame_time or (now - self.frame_time.value) > 0.5:
                    self.frame_ready.wait()
            
            # make a copy of the cropped frame
            with self.frame_lock:
                cropped_frame = self.shared_frame[self.region_y_offset:self.region_y_offset+self.region_size, self.region_x_offset:self.region_x_offset+self.region_size].copy()
                frame_time = self.frame_time.value

#            # Resize to 300x300 if needed
#            if cropped_frame.shape != (300, 300, 3):
#                cropped_frame = cv2.resize(cropped_frame, dsize=(300, 300), interpolation=cv2.INTER_LINEAR)
#           # Expand dimensions since the model expects images to have shape: [1, 300, 300, 3]
#           frame_expanded = np.expand_dims(cropped_frame, axis=0)

            img = Image.fromarray(cropped_frame)
            # add the frame to the queue
            if not self.prepped_frame_queue.full():
                self.prepped_frame_queue.put({
                    'camera_name': self.camera_name,
                    'frame_time': frame_time,
#                    'frame': frame_expanded.flatten().copy(),
                    'img': img,
                    'region_size': self.region_size,
                    'region_threshold': self.region_threshold,
                    'region_x_offset': self.region_x_offset,
                    'region_y_offset': self.region_y_offset,
                    'save_samples': self.region_save_samples
                })
            else:
                print("queue full. moving on")
