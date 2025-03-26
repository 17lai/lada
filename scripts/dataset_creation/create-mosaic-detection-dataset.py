import argparse
import os
import random
import shutil
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from os import path as osp
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

from lada.lib import visualization_utils, image_utils, transforms as lada_transforms
from lada.lib.nsfw_frame_detector import NsfwImageDetector, NsfwFrame
from lada.lib.threading_utils import clean_up_completed_futures
from lada.lib.ultralytics_utils import disable_ultralytics_telemetry, convert_binary_mask_to_yolo_detection_labels, convert_segment_masks_to_yolo_segmentation_labels

disable_ultralytics_telemetry()

from torchvision.transforms import transforms as torchvision_transforms

from lada.lib.image_utils import UnsharpMaskingSharpener
from lada.lib.jpeg_utils import DiffJPEG

def get_target_shape(img, target_size: int):
    h, w = img.shape[:2]
    new_w, new_h = (int(target_size * w / h), target_size) if h > w else (target_size, int(target_size * h / w))
    return new_h, new_w

def _create_realesrgan_degradation_pipeline(img, target_size, mosaic_size, device, p:float):
    target_h, target_w = get_target_shape(img, target_size)

    if not np.random.uniform() < p:
        return torchvision_transforms.Resize(size=(target_h, target_w))

    sharpener = UnsharpMaskingSharpener().to(device)
    jpeger = DiffJPEG(differentiable=False).to(device)
    kernel_range = [2 * v + 1 for v in range(3, 5)]

    small_mosaic_blocks =mosaic_size < min(img.shape[:2]) * 18 / 1000
    low_resolution_image = min(img.shape[:2]) < 700
    if small_mosaic_blocks or low_resolution_image:
        # skip heavy degradations
        first_pass = lambda img: img
    else:
        first_pass = torchvision_transforms.Compose([
            lada_transforms.Blur(kernel_range=kernel_range, kernel_list=['iso', 'aniso', 'generalized_iso', 'generalized_aniso', 'plateau_iso', 'plateau_aniso'], kernel_prob=[0.45, 0.25, 0.12, 0.03, 0.12, 0.03],
                                 sinc_prob=0.1, blur_sigma=[0.2, 2], betag_range=[0.5, 4], betap_range=[1, 2], device=device, p=0.8),
            lada_transforms.Resize(resize_range=[0.75, 1.25], resize_prob=[0.2, 0.7, 0.1], target_base_h=target_h, target_base_w=target_w, p=0.8),
            lada_transforms.GaussianPoissonNoise(sigma_range=[0., 4.], poisson_scale_range=[0., 0.5], gaussian_noise_prob=0.5, gray_noise_prob=0.4, p=0.8),
            lada_transforms.JPEGCompression(jpeger, jpeg_range=[45, 95], p=0.7),
        ])

    return torchvision_transforms.Compose([
        torchvision_transforms.Resize(size=(target_h, target_w)),
        lada_transforms.Sharpen(sharpener, p=0.5),
        first_pass,
        lada_transforms.Blur(kernel_range=kernel_range, kernel_list=['iso', 'aniso', 'generalized_iso', 'generalized_aniso', 'plateau_iso', 'plateau_aniso'], kernel_prob=[0.45, 0.25, 0.12, 0.03, 0.12, 0.03],
                             sinc_prob=0.1, blur_sigma=[0.1, 1.], betag_range=[0.5, 4], betap_range=[1, 2], device=device, p=0.8),
        lada_transforms.Resize(resize_range=[0.85, 1.15], resize_prob= [0.3, 0.4, 0.3], target_base_h=target_h, target_base_w=target_w, p=0.8),
        lada_transforms.GaussianPoissonNoise(sigma_range=[0., 2.], poisson_scale_range=[0., 0.3], gaussian_noise_prob=0.5, gray_noise_prob=0.4, p=0.8),
        torchvision_transforms.RandomChoice(transforms=[
            torchvision_transforms.Compose([
                lada_transforms.Resize(resize_range=[1., 1.], resize_prob=[0, 0, 1], target_base_h=target_h, target_base_w=target_w, p=1.0),
                lada_transforms.SincFilter(kernel_range=kernel_range, sinc_prob=0., device=device, p=0.),
                lada_transforms.JPEGCompression(jpeger, jpeg_range=[30, 95], p=0.7),
            ]),
            torchvision_transforms.Compose([
                lada_transforms.JPEGCompression(jpeger, jpeg_range=[45, 95], p=0.7),
                lada_transforms.Resize(resize_range=[1., 1.], resize_prob=[0, 0, 1], target_base_h=target_h, target_base_w=target_w, p=1.0),
                lada_transforms.SincFilter(kernel_range=kernel_range, sinc_prob=0., device=device, p=0.),
            ])
        ], p=[0.5, 0.5]),
    ])

def create_degradation_pipeline(hq_img, target_size, mosaic_size, device='cuda'):
    return torchvision_transforms.Compose([
        lada_transforms.Image2Tensor(bgr2rgb=False, unsqueeze=True, device=device),
        _create_realesrgan_degradation_pipeline(hq_img, target_size=target_size, mosaic_size=mosaic_size, device=device, p=0.8),
        lada_transforms.Tensor2Image(rgb2bgr=False, squeeze=True),
        lada_transforms.VideoCompression(p=0.3, codecs=['libx264', 'libx265'], codec_probs=[0.5, 0.5],
                                         crf_ranges={'libx264': (26, 32), 'libx265': (28, 34)},
                                         bitrate_ranges={}),
    ])

def process_image_file(file_path, output_root, nsfw_frame_generator: NsfwImageDetector, device='cpu', show=False, window_name="mosaic"):
    nsfw_frame: NsfwFrame = nsfw_frame_generator.detect(file_path)
    if not nsfw_frame:
        if not show:
            name = osp.splitext(os.path.basename(file_path))[0]
            shutil.copy(file_path, f"{output_root}/background_images/{name}.jpg")
        return

    img = nsfw_frame.frame
    mask = nsfw_frame.mask

    target_size = 640 # size of images what we'll train mosaic detection model with

    img_mosaic, mask_mosaic, mosaic_size = lada_transforms.Mosaic()(img, mask)
    degrade = create_degradation_pipeline(img, target_size=target_size, device=device, mosaic_size=mosaic_size)

    img_mosaic = degrade(img_mosaic)
    mask_mosaic = image_utils.resize(mask_mosaic, img_mosaic.shape[:2], interpolation=cv2.INTER_NEAREST)

    if show:
        show_img = visualization_utils.overlay_mask_boundary(img_mosaic, mask_mosaic, color=(0, 255, 0))
        mask = image_utils.resize(mask, img_mosaic.shape[:2], interpolation=cv2.INTER_NEAREST)
        show_img = visualization_utils.overlay_mask_boundary(show_img, mask, color=(255, 0, 0))

        cv2.imshow(window_name, show_img)

        while True:
            key_pressed = cv2.waitKey(1)
            if key_pressed & 0xFF == ord("n"):
                break
    else:
        name = osp.splitext(os.path.basename(file_path))[0]
        cv2.imwrite(f"{output_root}/images/{name}.jpg", img_mosaic, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        cv2.imwrite(f"{output_root}/masks/{name}.png", mask_mosaic)

def get_files(dir, filter_func):
    file_list = []
    for r, d, f in os.walk(dir):
        for file in f:
            file_path = osp.join(r, file)
            if filter_func(file_path):
                file_list.append(Path(file_path))
    return file_list

def parse_args():
    parser = argparse.ArgumentParser("Create mosaic detection dataset")
    parser.add_argument('--output-root', type=Path, help="directory where resulting images/masks are saved")
    parser.add_argument('--input-root', type=Path, help="directory containing image files")
    parser.add_argument('--device', type=str, default="cuda:0")
    parser.add_argument('--model', type=str, default="model_weights/lada_nsfw_detection_model_v1.3.pt", help="path to YOLO model")
    parser.add_argument('--workers', type=int, default=4, help="number of worker threads")
    parser.add_argument('--start-index', type=int, default=0, help="Can be used to continue a previous run. Note the index number next to last processed file name")
    parser.add_argument('--show', default=False, action=argparse.BooleanOptionalAction, help="show each sample")
    parser.add_argument('--max-file-limit', type=int, default=None, help="instead of processing all files found in input-root dir it will choose files randomly up to the given limit")

    args = parser.parse_args()
    return args

def main():
    args = parse_args()

    model = YOLO(args.model)
    nsfw_image_detector = NsfwImageDetector(model, args.device, random_extend_masks=True, conf=0.8)

    if not args.show:
        os.makedirs(f"{args.output_root}/masks", exist_ok=True)
        os.makedirs(f"{args.output_root}/images", exist_ok=True)
        os.makedirs(f"{args.output_root}/background_images", exist_ok=True)
        os.makedirs(f"{args.output_root}/detection_labels", exist_ok=True)
        os.makedirs(f"{args.output_root}/segmentation_labels", exist_ok=True)
        jobs = []

    selected_files = get_files(args.input_root, image_utils.is_image_file)
    if args.max_file_limit and len(selected_files) > args.max_file_limit:
        selected_files = random.choices(selected_files, k=args.max_file_limit)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for file_idx, file_path in enumerate(selected_files):
            if file_idx < args.start_index or len(list(args.output_root.glob(f"*/{file_path.name}*"))) > 0:
                print(f"{file_idx}, Skipping {file_path.name}: Already processed")
                continue
            print(f"{file_idx}, Processing {file_path.name}")
            if args.show:
                process_image_file(file_path, args.output_root, nsfw_image_detector, device=args.device, show=True)
            else:
                jobs.append(executor.submit(process_image_file, file_path, args.output_root, nsfw_image_detector, args.device))
                clean_up_completed_futures(jobs)
    wait(jobs, return_when=ALL_COMPLETED)
    clean_up_completed_futures(jobs)

    print("Finished processing images. Now converting dataset to YOLO format...")

    if not args.show:
        pixel_to_class_mapping = {255: 0}
        convert_segment_masks_to_yolo_segmentation_labels(f"{args.output_root}/masks", f"{args.output_root}/segmentation_labels", pixel_to_class_mapping)
        convert_binary_mask_to_yolo_detection_labels(f"{args.output_root}/masks", f"{args.output_root}/detection_labels", pixel_to_class_mapping)

    if args.show:
        cv2.destroyAllWindows()


if __name__ == '__main__':
    main()