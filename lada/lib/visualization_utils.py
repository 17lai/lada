import cv2
from lada.lib import image_utils
from lada.lib import Image
from lada.lib.mosaic_detector import Clip

def overlay_mask(frame, mask):
    overlay = cv2.cvtColor(mask, cv2.COLOR_GRAY2RGB)
    #overlay[:,:,1][overlay[:,:,1] > 0] = 30 # add some color
    output = frame.copy()
    alpha = 0.1
    cv2.addWeighted(overlay, alpha, output, 1 - alpha, 0, output)
    return output

def overlay_mask_boundary(frame, mask, color=(0, 255, 0)):
    output = frame.copy()
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cv2.drawContours(output, contours, -1, color, 2)
    return output

def overlay_edges(frame, edges, color=(0, 255, 0)):
    output = frame.copy()
    output[edges.squeeze() > 0] = color
    return output

def overlay(background, overlay):
    if overlay.ndim == 2:
        overlay = cv2.cvtColor(background, cv2.COLOR_GRAY2RGB)
    output = background.copy()
    alpha = 0.2
    cv2.addWeighted(overlay, alpha, output, 1 - alpha, 0, output)
    return output

def draw_box(img, box, color=(255, 0, 0), thickness = 2):
    start_point, end_point = (box[1], box[0]), (box[3], box[2])
    cv2.rectangle(img, start_point, end_point, color, thickness)

def draw_text(text, position, output, font_scale=0.5):
    cv2.putText(output, text, position, cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), 4,
                cv2.LINE_AA)
    cv2.putText(output, text, position, cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), 2,
                cv2.LINE_AA)

def draw_mosaic_detections(clip: Clip, border_color = (255, 0, 255)) -> list[Image]:
    mosaic_detection_images = []
    box_border_thickness = 2
    border_thickness_half = box_border_thickness // 2
    for (cropped_img, cropped_mask, _, orig_crop_shape, pad_after_resize) in clip:
        mosaic_detection_img = cropped_img.copy()

        draw_text(f"c:{clip.id},f_start:{clip.frame_start}",(25, cropped_img.shape[1] // 2), mosaic_detection_img)

        mosaic_detection_img = image_utils.unpad_image(mosaic_detection_img, pad_after_resize)
        shape_before_resize = mosaic_detection_img.shape
        mosaic_detection_img = image_utils.resize(mosaic_detection_img, orig_crop_shape[:2])

        t, l, b, r = 0, 0, mosaic_detection_img.shape[0] - 1, mosaic_detection_img.shape[1] - 1
        border_box = t + border_thickness_half, l + border_thickness_half, b - border_thickness_half, r - border_thickness_half

        draw_box(mosaic_detection_img, border_box, color=border_color, thickness=box_border_thickness)

        mosaic_detection_img = image_utils.resize(mosaic_detection_img, shape_before_resize[:2])
        mosaic_detection_img = image_utils.pad_image_by_pad(mosaic_detection_img, pad_after_resize)

        assert mosaic_detection_img.shape == cropped_img.shape, "shapes of mosaic detection img and cropped img must match"

        mosaic_detection_img = overlay_mask_boundary(mosaic_detection_img, cropped_mask)

        mosaic_detection_images.append(mosaic_detection_img)
    return mosaic_detection_images