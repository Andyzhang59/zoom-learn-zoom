from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
import sys
PY3 = sys.version_info > (3,)
if PY3:
    from builtins import isinstance
else:
    from __builtin__ import isinstance

import cv2, PIL.Image
import numpy as np
from timeit import default_timer as timer

FOCAL_CODE = 37386

# 35mm equivalent focal length
def readFocal_pil(image_path):
	img = PIL.Image.open(image_path)
	exif_data = img._getexif()
	return exif_data[FOCAL_CODE][0]/exif_data[FOCAL_CODE][1]

# image_set: a list of images
def BgrToGray(image_set):
    img_num = len(image_set)
    image_gray_set = []
    for i in range (img_num):
        image_gray_i = cv2.cvtColor(image_set[i], cv2.COLOR_BGR2GRAY)
        image_gray_set.append(image_gray_i)
    return image_gray_set

def ImageToFloat(image):
    image = image.astype(np.float32) / 255
    return image

# check
def GetFeature(image, feature_type):
    # Initiate detector
    if feature_type is "ORB":
        feat = cv2.ORB_create()
    elif feature_type is "SURF":
        feat = cv2.SURF(400)
    else:
        feat = cv2.SIFT()

    # find the keypoints
    key_pts = feat.detect(image, None)
    # compute the descriptors with ORB
    key_pts, key_descriptors = feat.compute(image, key_pts)  # key_pts correspondes to struct in MATLAB
    return key_pts, key_descriptors

# check
def GetTransform(ref_descriptors, ref_pts, image_set, t_type):
    if t_type is None:
        t_type = "homography"

    img_num = len(image_set)
    tform_set = []
    for i in range (img_num):
        print("[!] Processing frame %d" % i)
        base_image = image_set[i]
        query_pts, query_descriptors = GetFeature(base_image, "ORB")
        bf_matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches_features = bf_matcher.match(query_descriptors, ref_descriptors) # query to train
        avg_distance = 1e-6 + sum(m.distance for m in matches_features)/len(matches_features)
        matches_good = []
        for m in matches_features:
            if m.distance < avg_distance * 1. / 3:
                matches_good.append(m)

        num_matches_good = len(matches_good)
        print("Extracted %d good matches" % num_matches_good)
        query_match_pts = np.array(list(query_pts[m.queryIdx].pt for m in matches_good))
        ref_match_pts = np.array(list(ref_pts[m.trainIdx].pt for m in matches_good))
        ratio = max(ref_match_pts.max(), query_match_pts.max())//255 + 1
        query_match_pts_scale = (query_match_pts/ratio).astype(np.uint8).reshape(2,num_matches_good)
        ref_match_pts_scale = (ref_match_pts/ratio).astype(np.uint8).reshape(2,num_matches_good)

        if t_type == "rigid":
            tform = cv2.estimateRigidTransform(query_match_pts_scale, ref_match_pts_scale, True) # query to train
        elif t_type == "homography":
            tform, status = cv2.findHomography(query_match_pts, ref_match_pts)
        else:
            tform = cv2.estimateRigidTransform(query_match_pts_scale, ref_match_pts_scale, False)

        tform_set.append(tform)
    return tform_set

# check
def ApplyTransform(image_set, tform_set, t_type):
    if t_type is None:
        if tform_set[0].shape == 2:
            t_type = "rigid"
        elif tform_set[0].shape == 3:
            t_type = "homography"
        else:
            print("[x] Invalid transforms")
            exit()

    r, c = image_set[0].shape[0:2]
    img_num = len(image_set)
    image_t_set = np.zeros_like(image_set)
    for i in range(img_num):
        image_i = image_set[i]
        if t_type != "homography":
            image_i_transform = cv2.warpAffine(image_i, tform_set[i], (c, r),
                                                flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
        else:
            image_i_transform = cv2.warpPerspective(image_i, tform_set[i], (c, r),
                                                flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
        image_t_set[i] = image_i_transform

    return image_t_set

def SumAlignedImage(image_aligned, image_set):
    sum_img = np.float32(image_aligned[0]) * 1. / len(image_aligned)
    sum_img_t = np.float32(image_aligned[0]) * 1. / len(image_aligned)
    for i in range(1, len(image_aligned)):
        sum_img_t += np.float32(image_aligned[i]) * 1. / len(image_aligned)
        sum_img += np.float32(image_set[i]) * 1. / len(image_aligned)
    return sum_img_t, sum_img

def AlignEcc(image_set, images_gray_set, ref_ind, thre=0.05):
    img_num = len(image_set)
    # select the image as reference
    # ref_image = image_set[ref_ind]
    ref_gray_image = images_gray_set[ref_ind]
    r, c = image_set[0].shape[0:2]

    warp_mode = cv2.MOTION_AFFINE
    # cv2.MOTION_HOMOGRAPHY # cv2.MOTION_AFFINE # cv2.MOTION_TRANSLATION # cv2.MOTION_EUCLIDEAN

    # Define 2x3 or 3x3 matrices and initialize the matrix to identity
    if  warp_mode == cv2.MOTION_HOMOGRAPHY:
        print("Using homography model for alignment")
        identity_transform = np.eye(3, 3, dtype=np.float32)
        warp_matrix = np.eye(3, 3, dtype=np.float32)
        tform_set_init = [np.eye(3, 3, dtype=np.float32)] * img_num
    else:
        identity_transform = np.eye(2, 3, dtype=np.float32)
        warp_matrix = np.eye(2, 3, dtype=np.float32)
        tform_set_init = [np.eye(2, 3, dtype=np.float32)] * img_num

    number_of_iterations = 500
    termination_eps = 1e-6
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, number_of_iterations, termination_eps)

    # Run the ECC algorithm. The results are stored in warp_matrix.
    aligned_images = np.zeros_like(image_set)
    tform_set = np.zeros_like(tform_set_init)
    tform_inv_set = np.zeros_like(tform_set_init)
    valid_id = []
    motion_thre = thre * min(r, c)
    for i in range(ref_ind - 1, -1, -1):
        # s_i = timer()
        # print("Align image " + str(i))
        _, warp_matrix = cv2.findTransformECC(ref_gray_image, images_gray_set[i], warp_matrix, warp_mode, criteria)
        tform_set[i] = warp_matrix
        tform_inv_set[i] = cv2.invertAffineTransform(warp_matrix)

        motion_val = abs(warp_matrix - identity_transform).sum()
        if motion_val < motion_thre:
            valid_id.append(i)
        else:
            continue

        # e_i = timer()
        # print("each iter:", str(e_i - s_i))

    if  warp_mode == cv2.MOTION_HOMOGRAPHY:
        warp_matrix = np.eye(3, 3, dtype=np.float32)
    else:
        warp_matrix = np.eye(2, 3, dtype=np.float32)

    for i in range(ref_ind, img_num, 1):
        # s_i = timer()
        # print("Align image " + str(i))
        _, warp_matrix = cv2.findTransformECC(ref_gray_image, images_gray_set[i], warp_matrix, warp_mode, criteria)
        tform_set[i] = warp_matrix
        tform_inv_set[i] = cv2.invertAffineTransform(warp_matrix)

        motion_val = abs(warp_matrix - identity_transform).sum()
        if motion_val < motion_thre:
            valid_id.append(i)
        else:
            continue

        # e_i = timer()
        # print("each iter:", str(e_i - s_i))
    return tform_set, tform_inv_set, valid_id