import os

import cv2
import mediapipe as mp #face detector
import math
import numpy as np
import time

import warnings
warnings.simplefilter("ignore", UserWarning)

import torch
import torch.nn as  nn
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms

########################################################################
#-----------------------------------------------------------------------
# Model Architectures

class Bottleneck(nn.Module):
    expansion = 4
    def __init__(self, in_channels, out_channels, i_downsample=None, stride=1):
        super(Bottleneck, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride, padding=0, bias=False)
        self.batch_norm1 = nn.BatchNorm2d(out_channels, eps=0.001, momentum=0.99)

        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, padding='same', bias=False)
        self.batch_norm2 = nn.BatchNorm2d(out_channels, eps=0.001, momentum=0.99)

        self.conv3 = nn.Conv2d(out_channels, out_channels*self.expansion, kernel_size=1, stride=1, padding=0, bias=False)
        self.batch_norm3 = nn.BatchNorm2d(out_channels*self.expansion, eps=0.001, momentum=0.99)

        self.i_downsample = i_downsample
        self.stride = stride
        self.relu = nn.ReLU()

    def forward(self, x):
        identity = x.clone()
        x = self.relu(self.batch_norm1(self.conv1(x)))

        x = self.relu(self.batch_norm2(self.conv2(x)))

        x = self.conv3(x)
        x = self.batch_norm3(x)

        #downsample if needed
        if self.i_downsample is not None:
            identity = self.i_downsample(identity)
        #add identity
        x+=identity
        x=self.relu(x)

        return x

class Conv2dSame(torch.nn.Conv2d):

    def calc_same_pad(self, i: int, k: int, s: int, d: int) -> int:
        return max((math.ceil(i / s) - 1) * s + (k - 1) * d + 1 - i, 0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        ih, iw = x.size()[-2:]

        pad_h = self.calc_same_pad(i=ih, k=self.kernel_size[0], s=self.stride[0], d=self.dilation[0])
        pad_w = self.calc_same_pad(i=iw, k=self.kernel_size[1], s=self.stride[1], d=self.dilation[1])

        if pad_h > 0 or pad_w > 0:
            x = F.pad(
                x, [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2]
            )
        return F.conv2d(
            x,
            self.weight,
            self.bias,
            self.stride,
            self.padding,
            self.dilation,
            self.groups,
        )

class ResNet(nn.Module):
    def __init__(self, ResBlock, layer_list, num_classes, num_channels=3):
        super(ResNet, self).__init__()
        self.in_channels = 64

        self.conv_layer_s2_same = Conv2dSame(num_channels, 64, 7, stride=2, groups=1, bias=False)
        self.batch_norm1 = nn.BatchNorm2d(64, eps=0.001, momentum=0.99)
        self.relu = nn.ReLU()
        self.max_pool = nn.MaxPool2d(kernel_size = 3, stride=2)

        self.layer1 = self._make_layer(ResBlock, layer_list[0], planes=64, stride=1)
        self.layer2 = self._make_layer(ResBlock, layer_list[1], planes=128, stride=2)
        self.layer3 = self._make_layer(ResBlock, layer_list[2], planes=256, stride=2)
        self.layer4 = self._make_layer(ResBlock, layer_list[3], planes=512, stride=2)

        self.avgpool = nn.AdaptiveAvgPool2d((1,1))
        self.fc1 = nn.Linear(512*ResBlock.expansion, 512)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Linear(512, num_classes)

    def extract_features(self, x):
        x = self.relu(self.batch_norm1(self.conv_layer_s2_same(x)))
        x = self.max_pool(x)
        # print(x.shape)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = x.reshape(x.shape[0], -1)
        x = self.fc1(x)
        return x

    def forward(self, x):
        x = self.extract_features(x)
        x = self.relu1(x)
        x = self.fc2(x)
        return x

    def _make_layer(self, ResBlock, blocks, planes, stride=1):
        ii_downsample = None
        layers = []

        if stride != 1 or self.in_channels != planes*ResBlock.expansion:
            ii_downsample = nn.Sequential(
                nn.Conv2d(self.in_channels, planes*ResBlock.expansion, kernel_size=1, stride=stride, bias=False, padding=0),
                nn.BatchNorm2d(planes*ResBlock.expansion, eps=0.001, momentum=0.99)
            )

        layers.append(ResBlock(self.in_channels, planes, i_downsample=ii_downsample, stride=stride))
        self.in_channels = planes*ResBlock.expansion

        for i in range(blocks-1):
            layers.append(ResBlock(self.in_channels, planes))

        return nn.Sequential(*layers)

def ResNet50(num_classes, channels=3):
    return ResNet(Bottleneck, [3,4,6,3], num_classes, channels)


class LSTMPyTorch(nn.Module):
    def __init__(self):
        super(LSTMPyTorch, self).__init__()

        self.lstm1 = nn.LSTM(input_size=512, hidden_size=512, batch_first=True, bidirectional=False)
        self.lstm2 = nn.LSTM(input_size=512, hidden_size=256, batch_first=True, bidirectional=False)
        self.fc = nn.Linear(256, 7)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, x):
        x, _ = self.lstm1(x)
        x, _ = self.lstm2(x)
        x = self.fc(x[:, -1, :])
        x = self.softmax(x)
        return x


#-----------------------------------------------------------------------#
#########################################################################
#|||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||#
#########################################################################
#-----------------------------------------------------------------------#
# Sub functions
def pth_processing(fp):
    class PreprocessInput(torch.nn.Module):
        def init(self):
            super(PreprocessInput, self).init()

        def forward(self, x):
            x = x.to(torch.float32)
            x = torch.flip(x, dims=(0,))
            x[0, :, :] -= 91.4953
            x[1, :, :] -= 103.8827
            x[2, :, :] -= 131.0912
            return x

    def get_img_torch(img):

        ttransform = transforms.Compose([
            transforms.PILToTensor(),
            PreprocessInput()
        ])
        img = img.resize((224, 224), Image.Resampling.NEAREST)
        img = ttransform(img)
        img = torch.unsqueeze(img, 0)
        return img
    return get_img_torch(fp)

def tf_processing(fp):
    def preprocess_input(x):
        x_temp = np.copy(x)
        x_temp = x_temp[..., ::-1]
        x_temp[..., 0] -= 91.4953
        x_temp[..., 1] -= 103.8827
        x_temp[..., 2] -= 131.0912
        return x_temp

    def get_img_tf(img):
        img = cv2.resize(img, (224,224), interpolation=cv2.INTER_NEAREST)
        img = tf.keras.utils.img_to_array(img)
        img = preprocess_input(img)
        img = np.array([img])
        return img

    return get_img_tf(fp)

def norm_coordinates(normalized_x, normalized_y, image_width, image_height):

    x_px = min(math.floor(normalized_x * image_width), image_width - 1)
    y_px = min(math.floor(normalized_y * image_height), image_height - 1)

    return x_px, y_px

def get_box(fl, w, h):
    idx_to_coors = {}
    for idx, landmark in enumerate(fl.landmark):
        landmark_px = norm_coordinates(landmark.x, landmark.y, w, h)

        if landmark_px:
            idx_to_coors[idx] = landmark_px

    x_min = np.min(np.asarray(list(idx_to_coors.values()))[:,0])
    y_min = np.min(np.asarray(list(idx_to_coors.values()))[:,1])
    endX = np.max(np.asarray(list(idx_to_coors.values()))[:,0])
    endY = np.max(np.asarray(list(idx_to_coors.values()))[:,1])

    (startX, startY) = (max(0, x_min), max(0, y_min))
    (endX, endY) = (min(w - 1, endX), min(h - 1, endY))

    return startX, startY, endX, endY

def display_EMO_PRED(img, box, label='', color=(128, 128, 128), txt_color=(255, 255, 255), line_width=2, ):
    lw = line_width or max(round(sum(img.shape) / 2 * 0.003), 2)
    text2_color = (255, 0, 255)
    p1, p2 = (int(box[0]), int(box[1])), (int(box[2]), int(box[3]))
    cv2.rectangle(img, p1, p2, text2_color, thickness=lw, lineType=cv2.LINE_AA)
    font = cv2.FONT_HERSHEY_SIMPLEX

    tf = max(lw - 1, 1)
    text_fond = (0, 0, 0)
    text_width_2, text_height_2 = cv2.getTextSize(label, font, lw / 3, tf)
    text_width_2 = text_width_2[0] + round(((p2[0] - p1[0]) * 10) / 360)
    center_face = p1[0] + round((p2[0] - p1[0]) / 2)

    cv2.putText(img, label,
                (center_face - round(text_width_2 / 2), p1[1] - round(((p2[0] - p1[0]) * 20) / 360)), font,
                lw / 3, text_fond, thickness=tf, lineType=cv2.LINE_AA)
    cv2.putText(img, label,
                (center_face - round(text_width_2 / 2), p1[1] - round(((p2[0] - p1[0]) * 20) / 360)), font,
                lw / 3, text2_color, thickness=tf, lineType=cv2.LINE_AA)
    return img

def display_FPS(img, text, margin=1.0, box_scale=1.0):  # frame = display_FPS(frame, 'FPS: {0:.1f}'.format(1 / (t2 - t1)), box_scale=.5)
    img_h, img_w, _ = img.shape
    line_width = int(min(img_h, img_w) * 0.001)  # line width
    thickness = max(int(line_width / 3), 1)  # font thickness

    font_face = cv2.FONT_HERSHEY_SIMPLEX
    font_color = (0, 0, 0)
    font_scale = thickness / 1.5

    t_w, t_h = cv2.getTextSize(text, font_face, font_scale, None)[0]

    margin_n = int(t_h * margin)
    sub_img = img[0 + margin_n: 0 + margin_n + t_h + int(2 * t_h * box_scale),
              img_w - t_w - margin_n - int(2 * t_h * box_scale): img_w - margin_n]

    white_rect = np.ones(sub_img.shape, dtype=np.uint8) * 255

    img[0 + margin_n: 0 + margin_n + t_h + int(2 * t_h * box_scale),
    img_w - t_w - margin_n - int(2 * t_h * box_scale):img_w - margin_n] = cv2.addWeighted(sub_img, 0.5, white_rect, .5,
                                                                                          1.0)

    cv2.putText(img=img,
                text=text,
                org=(img_w - t_w - margin_n - int(2 * t_h * box_scale) // 2,
                     0 + margin_n + t_h + int(2 * t_h * box_scale) // 2),
                fontFace=font_face,
                fontScale=font_scale,
                color=font_color,
                thickness=thickness,
                lineType=cv2.LINE_AA,
                bottomLeftOrigin=False)

    return img
#-----------------------------------------------------------------------#
#########################################################################
#|||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||#
#########################################################################
#-----------------------------------------------------------------------#
# Testing Models:


# From_Krystia_Streamlit_image_or_video
    # image:
        # variable name: image_file
        # contents:
            # ① image_captured = camera_input
            # ② uploaded_image = image upload from user
        # video:
        # variable name: video_file

mp_face_mesh = mp.solutions.face_mesh

name_backbone_model = 'FER_static_ResNet50_AffectNet.pt'
# name_LSTM_model = 'IEMOCAP'
# name_LSTM_model = 'CREMA-D'
# name_LSTM_model = 'RAMAS'
# name_LSTM_model = 'RAVDESS'
# name_LSTM_model = 'SAVEE'
name_LSTM_model = 'Aff-Wild2'

# torch

pth_backbone_model = ResNet50(7, channels=3)
pth_backbone_model.load_state_dict(torch.load(name_backbone_model))
pth_backbone_model.eval()

pth_LSTM_model = LSTMPyTorch()
pth_LSTM_model.load_state_dict(torch.load('FER_dinamic_LSTM_{0}.pt'.format(name_LSTM_model)))
pth_LSTM_model.eval()


DICT_EMO = {
    0: 'Neutral',
    1: 'Happiness',
    2: 'Sadness',
    3: 'Surprise',
    4: 'Fear',
    5: 'Disgust',
    6: 'Anger'
}

#-----------------------------------------------------------------------#
#########################################################################
#|||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||#
#########################################################################
#-----------------------------------------------------------------------#
# Testing Models by image file

######################################################
#----------------------------------------------------#


input_file = image_file
dot_locate = input_file.find('.')


readable_imgs = [
    ".bmp", ".dib",     # Windows bitmaps – *.bmp, *.dib
    ".jpeg", ".jpg" ,   # JPEG files – *.jpeg, *.jpg
    ".png",             # Portable Network Graphics – *.png
    ".webp",            # WebP – *.webp
    ".sr", ".ras",      # Sun rasters – *.sr, *.ras
    ".tiff", "tif"      # TIFF files – *.tiff, *.tif
    # Raster and Vector geospatial data supported by GDAL
]

# If input is an picture:
if input_file[dot_locate:] in readable_imgs:
    #
    # https://www.geeksforgeeks.org/reading-image-opencv-using-python/

    # assign an image array to img variable
    cap_file = cv2.imread(input_file, cv2.IMREAD_COLOR)

    # print(cap_file.shape) # image shape attribute

    # Displaying the image:
    # Creating GUI window to display an image on screen
    # cv2.imshow(<title:str>, <image_array>)
    cv2.imshow('image', cap_file)

    # To hold the window on screen, we use cv2.waitKey method
    # Once it detected the close input, it will release the control
    # To the next line
    # First Parameter is for holding screen for specified milliseconds
    # It should be positive integer. If 0, then it will
    # hold the screen until user close it.
    cv2.waitKey(0)

    # cap = cap_file

    ########################
    #^^^^^^^^^^^^^^^^^^^^^^^
    with mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=False,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5) as face_mesh:
        print("Reading image started")

        # while cap.isOpened():
        #     print("Reading...")
            # t1 = time.time()  #----------------------------------------------------# https://docs.python.org/3/library/time.htlm#time.time
            # success, frame = cap.read()
            # if frame is None: #break
                ######################################################
                #----------------------------------------------------#
                # print("None: no webcam?")
                #----------------------------------------------------#
                ######################################################
    # break
    cap = cap_file

            # frame_copy = frame.copy()
            # frame_copy.flags.writeable = False
    frame_copy = cv2.cvtColor(cap, cv2.COLOR_BGR2RGB)
    results = face_mesh.process(frame_copy)
    frame_copy.flags.writeable = True

    if results.multi_face_landmarks:
        for fl in results.multi_face_landmarks:
            startX, startY, endX, endY  = get_box(fl, w, h)
            cur_face = frame_copy[startY:endY, startX: endX]

            cur_face = pth_processing(Image.fromarray(cur_face))
            features = torch.nn.functional.relu(pth_backbone_model.extract_features(cur_face)).detach().numpy()

            if len(lstm_features) == 0:
                lstm_features = [features]*10
            else:
                lstm_features = lstm_features[1:] + [features]

            lstm_f = torch.from_numpy(np.vstack(lstm_features))
            lstm_f = torch.unsqueeze(lstm_f, 0)
            output = pth_LSTM_model(lstm_f).detach().numpy()

            cl = np.argmax(output)
            label = DICT_EMO[cl]

            ######################################################
            #----------------------------------------------------#
            # dictionary histogram of the indentified face emotion
            video_emotions[label] = video_emotions.get(label, 0) + 1

            # average the ??probability?? of emotion
            emotions_weight[label] = emotions_weight.get(label, []) + [round(output[0][cl], 2)]
            print(label)
            #----------------------------------------------------#
            ######################################################

            frame = display_EMO_PRED(frame, (startX, startY, endX, endY), label+' {0:.1%}'.format(output[0][cl]), line_width=3)

        t2 = time.time()  #----------------------------------------------------# https://docs.python.org/3/library/time.htlm#time.time

        # frame = display_FPS(frame, 'FPS: {0:.1f}'.format(1 / (t2 - t1)), box_scale=.5)  # Upper right corner tag display "FPS: x.x"


        vid_writer.write(frame)

        cv2.imshow('Webcam', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            #----------------------------------------------------# print("quit")
            break

    ######################################################
    #----------------------------------------------------#
    print("Streaming ended")
    print(f"video_emotions:\n{video_emotions}")
    print(f"emotions_weight:\n{emotions_weight}")
    for k,v in emotions_weight.items():
        print(k, v)
        print(f'Max emotion weight: {max(v)}')
    #----------------------------------------------------#
    ######################################################

    vid_writer.release()
    cap.release()
    cv2.destroyAllWindows()

    #_______________________
    ########################
#-----------------------------------------------------------------------#
#########################################################################
#|||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||#
#########################################################################
#-----------------------------------------------------------------------#
# Testing Models by video file

# If input is an video:
else:
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    # Daisuke (Feb. 15, 2024)
    time_limit = 10  # seconds
    start_time = time.time()
    #________________________________________
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


######################################################
#----------------------------------------------------#

    # video_file = 'IMG_0535.MOV'  # 'daijoubu-nihongo.mov'  # 'Replay.mov'  'IMG_5221.MOV'
    cap_file = cv2.VideoCapture(video_file)
    print(type(cap_file)) # <class 'cv2.VideoCapture'>

    print(cap_file.isOpened())
    # should return True

    # get the size (width and height), FPS (frames per second), and total number of frames:
    print('Frame width:', cap_file.get(cv2.CAP_PROP_FRAME_WIDTH))
    # Frame width: 720.0
    print('Frame height:', cap_file.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # Frame height: 1280.0
    print('Frames Per Second:', cap_file.get(cv2.CAP_PROP_FPS))
    # Frames Per Second: 30.03824313361919
    print('Frame count:', cap_file.get(cv2.CAP_PROP_FRAME_COUNT))
    # Frame count: 432.0

    # calculate the playback time of the video in seconds with Total frames / FPS
    print('playback time:', cap_file.get(cv2.CAP_PROP_FRAME_COUNT) / cap_file.get(cv2.CAP_PROP_FPS))
    # playback time: 14.381666666666668


    # or webcam
    # cap = cv2.VideoCapture(0)
    cap = cap_file
#----------------------------------------------------#
######################################################

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = np.round(cap.get(cv2.CAP_PROP_FPS))

    # ######################################################
    # #----------------------------------------------------#
    # # to save video file serially, run the following if statement instead of 【path_save_video = 'result.mp4'】
    # if 'result.mp4' in os.listdir():
    #     video_file_vers = 0
    #     for i in os.listdir():
    #         if i.startswith('result'):
    #             video_file_vers += 1

    #     path_save_video = f'result-{name_LSTM_model}-{video_file_vers}.mp4'
    # # path_save_video = f'result-{name_LSTM_model}.mp4'
    # #----------------------------------------------------#
    # ######################################################

    # vid_writer = cv2.VideoWriter(path_save_video, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))

    lstm_features = []

    ######################################################
    #----------------------------------------------------#
    # dictionary histogram of the indentified face emotion
    video_emotions = {}
    emotions_weight = {}
    #----------------------------------------------------#
    ######################################################

    with mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=False,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5) as face_mesh:
        print("Streaming started")
        while cap.isOpened():
            print("Streaming...")
            t1 = time.time()  #----------------------------------------------------# https://docs.python.org/3/library/time.htlm#time.time
            success, frame = cap.read()
            if frame is None: #break
                ######################################################
                #----------------------------------------------------#
                print("None: no webcam?")
                #----------------------------------------------------#
                ######################################################
                break

            frame_copy = frame.copy()
            frame_copy.flags.writeable = False
            frame_copy = cv2.cvtColor(frame_copy, cv2.COLOR_BGR2RGB)
            results = face_mesh.process(frame_copy)
            frame_copy.flags.writeable = True

            if results.multi_face_landmarks:
                for fl in results.multi_face_landmarks:
                    startX, startY, endX, endY  = get_box(fl, w, h)
                    cur_face = frame_copy[startY:endY, startX: endX]

                    cur_face = pth_processing(Image.fromarray(cur_face))
                    features = torch.nn.functional.relu(pth_backbone_model.extract_features(cur_face)).detach().numpy()

                    if len(lstm_features) == 0:
                        lstm_features = [features]*10
                    else:
                        lstm_features = lstm_features[1:] + [features]

                    lstm_f = torch.from_numpy(np.vstack(lstm_features))
                    lstm_f = torch.unsqueeze(lstm_f, 0)
                    output = pth_LSTM_model(lstm_f).detach().numpy()

                    cl = np.argmax(output)
                    label = DICT_EMO[cl]

                    ######################################################
                    #----------------------------------------------------#
                    # dictionary histogram of the indentified face emotion
                    video_emotions[label] = video_emotions.get(label, 0) + 1

                    # average the ??probability?? of emotion
                    emotions_weight[label] = emotions_weight.get(label, []) + [round(output[0][cl], 2)]
                    print(label)
                    #----------------------------------------------------#
                    ######################################################

                    frame = display_EMO_PRED(frame, (startX, startY, endX, endY), label+' {0:.1%}'.format(output[0][cl]), line_width=3)

            t2 = time.time()  #----------------------------------------------------# https://docs.python.org/3/library/time.htlm#time.time

            # frame = display_FPS(frame, 'FPS: {0:.1f}'.format(1 / (t2 - t1)), box_scale=.5)  # Upper right corner tag display "FPS: x.x"


            vid_writer.write(frame)

            cv2.imshow('Webcam', frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                #----------------------------------------------------# print("quit")
                break

        ######################################################
        #----------------------------------------------------#
        print("Streaming ended")
        print(f"video_emotions:\n{video_emotions}")
        print(f"emotions_weight:\n{emotions_weight}")
        for k,v in emotions_weight.items():
            print(k, v)
            print(f'Max emotion weight: {max(v)}')
        #----------------------------------------------------#
        ######################################################

        vid_writer.release()
        cap.release()
        cv2.destroyAllWindows()

#-----------------------------------------------------------------------#
#########################################################################
#|||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||#
#########################################################################
#-----------------------------------------------------------------------#
