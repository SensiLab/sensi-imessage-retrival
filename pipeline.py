

import os
import re
import io
import base64
import argparse

import openai

from ocrmac import ocrmac
from doclayout_yolo import YOLOv10
from huggingface_hub import hf_hub_download
from PIL import Image

def _clip_box(x1: float, y1: float, x2: float, y2: float, width: int, height: int) -> tuple[int, int, int, int]:
    """
    Clip box coordinates to be within image bounds and ensure a minimum size of 1 pixel.
    @author: Stephen Krol

    :param x1: The x-coordinate of the top-left corner of the box.
    :type x1: float
    :param y1: The y-coordinate of the top-left corner of the box.
    :type y1: float
    :param x2: The x-coordinate of the bottom-right corner of the box.
    :type x2: float
    :param y2: The y-coordinate of the bottom-right corner of the box.
    :type y2: float
    :param width: The width of the image, used for clipping the x-coordinates.
    :type width: int
    :param height: The height of the image, used for clipping the y-coordinates.
    :type height: int

    :return: A tuple of integers representing the clipped box coordinates (left, top, right, bottom).
    :rtype: tuple[int, int, int, int]
    """
    left = max(0, min(int(round(x1)), width - 1))
    top = max(0, min(int(round(y1)), height - 1))
    right = max(left + 1, min(int(round(x2)), width))
    bottom = max(top + 1, min(int(round(y2)), height))

    return left, top, right, bottom

def _load_doclayout_model() -> YOLOv10:
    """
    Function loads the doclayout YOLO model for figure detection. It checks for the model path in the environment variable 
    'DOCLAYOUT_YOLO_MODEL' and initializes the model with that path. 
    @author: Stephen Krol

    :return: An instance of the YOLOv10 model initialized with the specified model path.
    :rtype: YOLOv10
    """

    filepath = hf_hub_download(repo_id="juliozhao/DocLayout-YOLO-DocStructBench", filename="doclayout_yolo_docstructbench_imgsz1024.pt")
    return YOLOv10(filepath)

def _clean_doclayout_results(predictions: dict, result: dict, width: int, height: int) -> dict:
    """
    Function cleans the raw results from the doclayout model by extracting only the relevant figure detections and their bounding boxes. 
    It returns a simplified dictionary containing only the figure information.

    :param predictions: The raw output from the doclayout model, which may contain various detected elements.
    :type predictions: dict
    :param result: The initial result dictionary that includes metadata about the image and model. This will be updated with the cleaned figure information.
    :type result: dict
    :param width: The width of the input image, used for clipping bounding box coordinates.
    :type width: int
    :param height: The height of the input image, used for clipping bounding box coordinates.
    :type height: int

    :return: A simplified dictionary containing only the figure information.
    :rtype: dict
    """

    pred = predictions[0]

    # check if 'boxes' attribute exists and has detections
    boxes = getattr(pred, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return result

    # extract relevant information about detected figures
    names = getattr(pred, "names", {}) or {}
    xyxy_list = boxes.xyxy.cpu().tolist() if hasattr(boxes.xyxy, "cpu") else boxes.xyxy.tolist() # coordinates of bounding boxes
    cls_list = boxes.cls.cpu().tolist() if hasattr(boxes.cls, "cpu") else boxes.cls.tolist() # class IDs of detected objects
    conf_list = boxes.conf.cpu().tolist() if hasattr(boxes.conf, "cpu") else boxes.conf.tolist() # confidence scores of detections

    regions = []
    ocr_snippets = []

    # iterate through detected boxes and extract figure information clipping to image boundaries
    for i, (xyxy, cls_id, score) in enumerate(zip(xyxy_list, cls_list, conf_list), start=1):

        class_idx = int(cls_id)

        if class_idx != 3:  # class ID 3 corresponds to 'figure' in the doclayout model
            continue

        x1, y1, x2, y2 = _clip_box(xyxy[0], xyxy[1], xyxy[2], xyxy[3], width, height)
        label = names.get(class_idx, f"class_{class_idx}")

        regions.append(
            {
                "id": i,
                "class_id": class_idx,
                "label": label,
                "confidence": round(float(score), 4),
                "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            }
        )

    regions.sort(key=lambda r: (r["bbox"]["y1"], r["bbox"]["x1"]))
    result["regions"] = regions
    result["ocr_text"] = "\n\n".join(ocr_snippets).strip()

    return result

def _classify_figure_with_gpt(image_bytes: bytes, mime: str, model: str, prompt: str) -> dict[str, Any]:
    """
    Function takes the bytes of an image crop and classifies it using gpt to determine whether it is a meaningful figure likely to contain relevant content, versus a profile photo, avatar, logo, icon, ad, navigation UI, button, chat bubble, or other decorative UI fragment that should be rejected. It returns a dictionary containing the classification decision and reasoning.
    @author: Stephen Krol

    :param image_bytes: The bytes of the image crop to classify.
    :type image_bytes: bytes
    :param mime: The MIME type of the image (e.g., "image/jpeg").
    :type mime: str
    :param model: The name of the gpt model to use for classification.
    :type model: str
    :param prompt: The prompt to use for the GPT model.
    :type prompt: str

    :return: A dictionary containing the classification decision, reason, and category.
    :rtype: dict[str, Any]
    """

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set in environment variables")

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}", "detail": "auto"},
                    },
                ],
            }
        ],
        max_tokens=200,
        temperature=0,
    )

    text = response.choices[0].message.content or ""

    category = "other"
    keep = "False"
    reason = "no reason"

    for line in text.splitlines():
        if line.startswith("CATEGORY:"):
            category = line.split(":", 1)[1].strip().lower()
        elif line.startswith("KEEP:"):
            keep = line.split(":", 1)[1].strip()
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    keep = True if keep == "True" else False
    reason = str(reason).strip() or "no reason"
    category = str(category).strip() or "other"

    return {"keep": keep, "reason": reason, "category": category}

def _crop_image(rgb: "Image.Image", left: int, top: int, right: int, bottom: int) -> bytes:
    """
    Function crops a region from the input image specified by the bounding box and saves it as a temporary file. 
    It returns the bytes of the cropped image. This function is used to extract figure crops from the source image for classification.
    @author: Stephen Krol

    :param rgb: The source image in RGB format.
    :type rgb: Image.Image
    :param left: The x-coordinate of the left edge of the cropping box.
    :type left: int
    :param top: The y-coordinate of the top edge of the cropping box.
    :type top: int
    :param right: The x-coordinate of the right edge of the cropping box.
    :type right: int
    :param bottom: The y-coordinate of the bottom edge of the cropping box.
    :type bottom: int

    :return: The bytes of the cropped image.
    :rtype: bytes
    """

    crop = rgb.crop((left, top, right, bottom))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=95)
    image_bytes = buf.getvalue()
    buf.close()

    return image_bytes
    
def filter_screenshot_figures_with_gpt(src_image_path: str,
                                       figures: list[bytes],
                                       model: str | None = None,
                                       write_image: bool = False) -> bytes:
    """
    If a screenshot, only one figure should be extracted. If there are multiple, ask GPT-4 which is more likely the main content.
    @author: Stephen Krol

    :param src_image_path: The file path to the original screenshot image.
    :type src_image_path: str
    :param figures: A list of byte arrays, each representing a cropped figure extracted from the screenshot.
    :type figures: list[bytes]
    :param model: The name of the gpt model to use for classification. If None, it will use the default model specified in the environment variable 'FIGURE_FILTER_MODEL' or fallback to 'gpt-4o-mini'.
    :type model: str | None
    :param write_image: Whether to write the filtered image to disk. Defaults to False.
    :type write_image: bool

    :return: The bytes of the figure that is classified as the main content of the screenshot.
    :rtype: bytes
    """
    
    # read src image and encode as base64
    with open(src_image_path, "rb") as f:
        src_image_b64 = base64.b64encode(f.read()).decode("utf-8")
    
    prompt = (
        "You are an assistant that helps filter multiple figure crops extracted from a screenshot to identify which one is most likely the main content. "
        "You will be given multiple images with the first being the original screenshot and the others being cropped figures extracted from it. "
        "Your task is to determine which cropped figure is the main content of the screenshot. YOU MUST NEVER CHOOSE THE ORIGINAL FIGURE."
        "ENSURE THAT THE DECISION IS ALWAYS A NUMBER. NEVER ADD ANYTHING OTHER THAN A NUMBER."
        "You must output the following format EXACTLY, DO NOT DEVIATE: "
        "DESCRIPTION: a one sentence description of the content of each figure and how it relates to the overall screenshot\n"
        "REASONING: a short explanation of how you determined which figure is the main content\n"
        "DECISION: the index (starting from 1) of the figure that is the main content"
        "Example response: "
        "DESCRIPTION: The original screenshot appears to be from a social media platform. Figure 1 is a cropped image of a person's profile photo. Figure 2 is a cropped image of the image from the post. Figure 3 contains the image but with a bit of the caption and platform buttons.\n"
        "REASONING: Figure 1 is just a profile photo and not the main content. Figure 3 contains extra UI elements and text, while Figure 2 is a clean crop of the main image content. Therefore, Figure 2 is most likely the main content.\n"
        "DECISION: 2"
    )

    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model or os.environ.get("FIGURE_FILTER_MODEL", "gpt-4o"),
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{src_image_b64}", "detail": "auto"},
                    },
                ] + [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(figure).decode('utf-8')}", "detail": "auto"},
                    }
                    for figure in figures
                ],
            }
        ],
        max_tokens=300,
        temperature=0,
    )

    # parse response to extract the index of the figure classified as main content
    text = response.choices[0].message.content or ""
    decision = None
    for line in text.splitlines():
        if line.startswith("DECISION:"):
            try:
                decision = int(line.split(":", 1)[1].strip())
            except ValueError:
                # raise ValueError(f"Invalid DECISION value in GPT response: {line}")
                print(f"Invalid DECISION value in GPT response: {line}")
                decision = 1

    if decision is None:
        print("No DECISION found in GPT response")
        decision = 1

    if decision < 1 or decision > len(figures):
        print(f"DECISION index {decision} is out of bounds, defaulting to 1")
        decision = 1

    if write_image:
        output_path = "filtered_figure.jpg"
        with open(output_path, "wb") as f:
            f.write(figures[decision - 1])
        print(f"Written filtered figure to: {output_path}")

    return figures[decision - 1]

def filter_figures_with_gpt(
    candidates: dict[str, Any],
    prompt: str,
    source_image_path: str | None = None,
    output_dir: str | None = None,
    model: str | None = None,
    write_image: bool = False) -> dict[str, Any]:
    """
    Function takes the output from the doclayout model and filters the detected figures using gpt to determine which ones are meaningful 
    content versus UI elements, profile photos, logos, ads, or other non-meaningful crops. It saves the meaningful figures to disk and returns a summary of the filtering results.
    @author: Stephen Krol

    :param candidates: A dictionary containing the source image path and detected figure candidates.
    :type candidates: dict[str, Any]
    :param prompt: The prompt to use for the GPT model.
    :type prompt: str
    :param source_image_path: The file path to the original image from which the figures were
    detected. This is used for cropping the figures. If None, the function will not attempt to crop and save figures.
    :type source_image_path: str | None
    :param output_dir: The directory where the filtered figure images should be saved. If None
    figures will not be saved to disk.
    :type output_dir: str | None
    :param model: The name of the gpt model to use for filtering decisions. If None, it will use the default model specified in the environment variable 'FIGURE_FILTER_MODEL' or fallback to 'gpt-4o-mini'.
    :type model: str | None
    :param write_image: Whether to write the filtered images to disk. Defaults to False.
    :type write_image: bool

    :return: A dictionary summarizing the filtering results, including counts of kept and rejected figures, paths to saved figures, and any errors encountered.
    :rtype: dict[str, Any]
    """

    source_image = candidates.get("source_image")
    figures = candidates.get("figures", [])
    base_name = os.path.splitext(os.path.basename(source_image))[0]
    target_dir = output_dir or os.path.join(os.path.dirname(source_image), f"{base_name}_figures")

    os.makedirs(target_dir, exist_ok=True)
    chosen_model = model or os.environ.get("FIGURE_FILTER_MODEL", "gpt-4o-mini")

    kept = []
    rejected = []
    errors = []

    # itereate through figures and crop from source image then classify with gpt to determine whether to keep or reject each figure, saving kept figures to disk and recording results
    with Image.open(source_image) as src:
        rgb = src.convert("RGB")
        for index, figure in enumerate(figures, start=1):
            bbox = figure.get("bbox") or {}
            left = int(bbox.get("x1", 0))
            top = int(bbox.get("y1", 0))
            right = int(bbox.get("x2", 0))
            bottom = int(bbox.get("y2", 0))

            image_bytes = _crop_image(rgb, left, top, right, bottom)

            try:
                decision = _classify_figure_with_gpt(image_bytes, "image/jpeg", chosen_model, prompt)
            except Exception as exc:
                raise RuntimeError(f"Error classifying figure with GPT: {exc}") from exc
                errors.append({"region_id": figure.get("region_id"), "error": str(exc)})
                continue

            # write to disk if kept
            enriched = {**figure, "gpt": decision, "bytes": image_bytes}
            filename = f"{base_name}_figure_{index:02d}_r{figure.get('region_id', index)}.jpg"
            destination = os.path.join(target_dir, filename)
            if decision["keep"]:

                if write_image:
                    with open(destination, "wb") as out:
                        out.write(image_bytes)

                enriched["path"] = destination
                kept.append(enriched)
            else:
                rejected.append(enriched)
                if write_image:
                    with open(destination, "wb") as out:
                        out.write(image_bytes)

    status = "ok" if not errors else ("partial" if kept or rejected else "error")

    return {
        "status": status,
        "model": chosen_model,
        "source_image": source_image,
        "output_dir": target_dir,
        "input_count": len(figures),
        "kept_count": len(kept),
        "rejected_count": len(rejected),
        "kept": kept,
        "rejected": rejected,
        "errors": errors,
    }

def identify_figures_from_image(image_path: str) -> list[dict]:
    """
    Function identifies and extracts figures from an image using the doclayout YOLO model. It takes the image path as input and returns a list of dictionaries, 
    each containing information about a detected figure such as its bounding box coordinates and confidence score.
    @author: Stephen Krol

    :param image_path: The file path to the image from which to extract figures.
    :type image_path: str

    :return: A list of dictionaries, each representing a detected figure with its bounding box and confidence score.
    :rtype: list[dict]
    """

    model = _load_doclayout_model()

    with Image.open(image_path) as img:
        width, height = img.size

    result: dict[str, Any] = {
        "status": "ok",
        "error": None,
        "image_path": image_path,
        "image_size": {"width": width, "height": height},
        "model": "DocLayout-YOLO",
        "regions": [],
    }
    
    device = os.environ.get("DOCLAYOUT_DEVICE", "cpu")
    conf = float(os.environ.get("DOCLAYOUT_CONF", "0.2"))
    imgsz = int(os.environ.get("DOCLAYOUT_IMGSZ", "1024"))

    predictions = model.predict(image_path, imgsz=imgsz, conf=conf, device=device)
    if not predictions:
        return result

    identified_figures = _clean_doclayout_results(predictions, result, width, height)

    image_path = identified_figures.get("image_path")
    regions = identified_figures.get("regions", [])

    # iterate over detected regions and extract those labeled as 'figure', clipping their bounding boxes to image boundaries
    candidates = []
    index = 1
    for region in regions:
        label = str(region.get("label", "")).lower()
        if "figure" not in label:
            continue

        bbox = region.get("bbox") or {}
        try:
            x1 = float(bbox["x1"])
            y1 = float(bbox["y1"])
            x2 = float(bbox["x2"])
            y2 = float(bbox["y2"])
        except Exception:
            continue

        left, top, right, bottom = _clip_box(x1, y1, x2, y2, width, height)
        region_id = region.get("id", index)
        candidates.append(
            {
                "region_id": region_id,
                "label": region.get("label"),
                "confidence": region.get("confidence"),
                "bbox": {"x1": left, "y1": top, "x2": right, "y2": bottom},
            }
        )
        index += 1

    return {
        "status": "ok",
        "source_image": image_path,
        "figure_count": len(candidates),
        "figures": candidates,
    }

def extract_text_from_image(image_path: str) -> str:
    """
    Function extracts text from image using MACOS built-in OCR capabilities via ocrmac library.
    It returns the extracted text as a string. If no text is detected, it returns an empty string.
    @author: Stephen Krol

    :param image_path: The file path to the image from which to extract text.
    :type image_path: str

    :return: A string containing the extracted text, or an empty string if no text is detected.
    :rtype: str
    """
    image = Image.open(image_path)
    extracted_text = ocrmac.text_from_image(image)

    return ' '.join([result[0] for result in extracted_text])

def extract_and_filter_figures(image_path: str, prompt: str) -> list[bytes]:
    """
    Function that takes an image path, identifies figures, filters them with gpt to find the main figure, then extracts text from that figure if it's a screenshot.
    It returns a list containing the filtered figure as bytes.
    @author: Stephen Krol

    :param image_path: The file path to the image to process.
    :type image_path: str
    :param prompt: The prompt to use for the GPT model when filtering figures.
    :type prompt: str
    
    :return: A list containing the bytes of the figure that is classified as the main content of the screenshot.
    :rtype: list[bytes]
    """

    extracted_figures = identify_figures_from_image(image_path)
    response = filter_figures_with_gpt(extracted_figures, prompt=prompt, source_image_path=image_path, output_dir=None, model=None, write_image=False)
    screenshot_figures = [kept["bytes"] for kept in response.get("kept", [])]

    return screenshot_figures

# TODO: could be replaced by a local model
# TODO: how does this change the text? is the language model adding anything?
def clean_text(text: str) -> str:
    """
    Function cleans the extracted text by passing to gpt to fix the cohesiveness without
    summarising or changing the text. It returns the cleaned text as a string.

    :param text: The raw extracted text to be cleaned.
    :type text: str

    :return: A cleaned version of the input text.
    :rtype: str
    """
    
    prompt = (
        "You are a helpful assistant that takes raw OCR text extracted from an image and cleans it up. "
        "Fix any issues with spacing, line breaks, and formatting to produce a more cohesive and readable version of the text. "
        "Do not summarise or change the content, just improve the formatting and readability. "
        "Return only the cleaned text without any explanations."
    )

    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        max_tokens=3000,
    )

    return response.choices[0].message.content or ""

def classify_image(image_path: str) -> str:
    """
    Function classifies an image using gpt as either:
    - "screenshot": a screenshot of a phonescreen, whether social media, messaging, or other app.
    - "presentation": a photo of a presentation slide.
    - "poster": a photo of a poster, flyer, or other printed material.
    - "book": a photo of a book page, article, or other printed text.
    - "other": any other type of image.
    It returns the predicted category as a string.
    @author: Stephen Krol

    :param image_path: The file path to the image to classify.
    :type image_path: str

    :return: The predicted category of the image.
    :rtype: str
    """

    # read and encode the image as base64
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")
    
    prompt = (
        "You are an assistant that classifies images into one of the following categories: "
        "- 'screenshot': a screenshot of a phonescreen, whether social media, messaging, or other app. "
        "- 'presentation': a photo of a presentation slide. "
        "- 'poster': a photo of a poster, flyer, or other printed material. "
        "- 'book': a photo of a book page, article, or other printed text. "
        "- 'other': any other type of image. "
        "Before classifying the image, analyse its content and describe it in a sentence. "
        "Then, based on that analysis, classify the content of this image into one of those categories and return only the category name as a single word."
        "Output format: "
        "DESCRIPTION: <a one sentence description of the image content>\n"
        "CATEGORY: <the predicted category>"
    )

    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}", "detail": "auto"},
                    },
                ],
            }],
        max_tokens=300,
    )

    text = response.choices[0].message.content or ""
    category = "other"
    for line in text.splitlines():
        if line.startswith("CATEGORY:"):
            category = line.split(":", 1)[1].strip().lower()
    
    if category not in ("screenshot", "presentation", "poster", "book", "other"):
        category = "other"
    
    return category

def text_extraction_pipeline(image_path: str) -> str:
    """
    Main function that takes an image path, classifies the image, and applies the appropriate pipeline to extract and clean text based on the predicted category. 
    It returns the cleaned extracted text as a string.
    """
    try:
        extracted_text = extract_text_from_image(image_path)
        if not extracted_text.strip():
            return ""
        cleaned_text = clean_text(extracted_text)
    except Exception as exc:
        print(f"Error extracting or cleaning text: {exc}")
        cleaned_text = ""

    return cleaned_text

def screenshot_pipeline(image_path: str) -> Tuple[bytes, str, bool]:
    """
    Function that takes an image path, identifies figures, filters them, and extracts text from the main figure if it's a screenshot. 
    It returns the cleaned extracted text from the main figure of the screenshot.
    @author: Stephen Krol

    :param image_path: The file path to the image to process.
    :type image_path: str

    :return: A tuple containing the filtered figure as bytes, the cleaned extracted text from the main figure of the screenshot, and a boolean indicating if the figure is the original screenshot.
    :rtype: Tuple[bytes, str, bool]
    """

    # extract text
    cleaned_text = text_extraction_pipeline(image_path)
    is_original_screenshot = False

    prompt = (
        "You are filtering screenshot crops extracted by a layout model. "
        "Keep only meaningful figures/images that are likely relevant content. "
        "Reject images that are ONLY profile photos, avatars, icons, logos, ads, navigation UI, buttons, chat bubbles, and decorative UI fragments. "
        "Images can contain these elements but if they are the main content of the crop and likely irrelevant to the overall screenshot, they should not be kept. "
        "Just text is also not meaningful as a figure. "
        "Respond with strict format as following:"
        "KEEP: True or False\n"
        "REASON: a short explanation of the decision\n"
        "CATEGORY: one of meaningful_figure, profile_or_avatar, ui_element, logo_or_icon, ad_or_promo, other\n"
        "Example response: "
        "KEEP: False\n"
        "REASON: this crop is a circular profile photo of a person, not a meaningful figure\n"
        "CATEGORY: profile_or_avatar"
    )
    try:
        # extract figures and filter with gpt to find the main figure, then extract text from that figure if it's a screenshot
        extracted_figures = extract_and_filter_figures(image_path, prompt)

        # should only be one screenshot
        if len(extracted_figures) > 1:
            filtered_figure = filter_screenshot_figures_with_gpt(image_path, extracted_figures, model=None, write_image=False)
        elif len(extracted_figures) == 1:
            filtered_figure = extracted_figures[0]
        else:
            # set to original figure as bytes
            with open(image_path, "rb") as f:
                filtered_figure = f.read()
            is_original_screenshot = True
    except Exception as exc:
        print(f"Error in screenshot pipeline: {exc}")
        with open(image_path, "rb") as f:
            filtered_figure = f.read()
        is_original_screenshot = True
    
    return filtered_figure, cleaned_text, is_original_screenshot

def presentation_pipeline(image_path: str) -> Tuple[bytes, str]:
    """
    Function that takes an image path, identifies figures, filters them, and extracts text from the main figure if it's a presentation slide. 
    It returns the cleaned extracted text from the main figure of the presentation slide in bytes.
    @author: Stephen Krol

    :param image_path: The file path to the image to process.
    :type image_path: str

    :return: A tuple containing the original figure as bytes and the cleaned extracted text from the main figure of the presentation slide.
    :rtype: Tuple[bytes, str]
    """

    # extract text
    cleaned_text = text_extraction_pipeline(image_path)

    with open(image_path, "rb") as f:
        original_figure = f.read()
    
    return original_figure, cleaned_text

def poster_pipeline(image_path: str) -> Tuple[bytes, str]:
    """
    Function that takes an image path and extracts text from the main figure if it's a poster. 
    It returns the cleaned extracted text from the main figure of the poster in bytes.
    @author: Stephen Krol

    :param image_path: The file path to the image to process.
    :type image_path: str

    :return: A tuple containing the original figure as bytes and the cleaned extracted text from the main figure of the poster.
    :rtype: Tuple[bytes, str]
    """

    # extract text
    cleaned_text = text_extraction_pipeline(image_path)

    with open(image_path, "rb") as f:
        original_figure = f.read()
    
    return original_figure, cleaned_text

def book_pipeline(image_path: str) -> Tuple[list[bytes], str, bool]:
    """
    Function that takes an image path and extracts text from the main figure if it's a book page. 
    It returns the cleaned extracted text from the main figures of the book page in bytes.
    @author: Stephen Krol
    """

    # extract text
    cleaned_text = text_extraction_pipeline(image_path)
    is_original_figure = False

    try:
         # extract figures and filter with gpt to find the main figure, then extract text from that figure if it's a book page
        extracted_figures = identify_figures_from_image(image_path)
        prompt = (
            "You are filtering figures of book page crops extracted by a layout model. "
            "Keep only crops that are mostly figures or images. Not not include crops of just or mostyly text "
            "Images can contain some text but if they are mostly text and not primarily a figure, they should not be kept. "
            "Respond with strict format as following for each figure:"
            "KEEP: True or False\n"
            "REASON: a short explanation of the decision\n"
            "CATEGORY: one of image, figure, table, text, other\n"
            "Example response: "
            "KEEP: True\n"
            "REASON: this crop contains a diagram that is relevant to the content of the page\n"
            "CATEGORY: figure\n"
            "KEEP: False\n"
            "REASON: this crop of mostly text on the page\n"
            "CATEGORY: text"
        )

        extracted_figures = extract_and_filter_figures(image_path, prompt)
    except Exception as exc:
        print(f"Error in book pipeline: {exc}")
        extracted_figures = []
        with open(image_path, "rb") as f:
            original_figure = f.read()
        extracted_figures.append(original_figure)
        is_original_figure = True
        
    return extracted_figures, cleaned_text, is_original_figure

def other_pipeline(image_path: str) -> Tuple[bytes, str]:
    """
    Function that takes an image path and extracts text if it's classified as 'other'. 
    It returns the cleaned extracted text from the image.
    @author: Stephen Krol

    :param image_path: The file path to the image to process.
    :type image_path: str

    :return: A tuple containing the original figure as bytes and the cleaned extracted text from the image.
    :rtype: Tuple[bytes, str]
    """

    # extract text
    cleaned_text = text_extraction_pipeline(image_path)

    with open(image_path, "rb") as f:
        original_figure = f.read()
    
    return original_figure, cleaned_text


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract text from an image.")
    parser.add_argument("--image_path", type=str, help="Path to the image file.")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        raise SystemExit("ERROR: 'python-dotenv' is required. Run: pip install python-dotenv")


    image_category = classify_image(args.image_path)
    print(f"Predicted image category: {image_category}")

    if image_category == "screenshot":
        filtered_figure, cleaned_text = screenshot_pipeline(args.image_path)
        print(cleaned_text)
        with open("filtered_figure.jpg", "wb") as f:
            f.write(filtered_figure)
    elif image_category == "presentation":
        original_figure, cleaned_text = presentation_pipeline(args.image_path)
        print(cleaned_text)
        with open("presentation_figure.jpg", "wb") as f:
            f.write(original_figure)
    elif image_category == "poster":
        original_figure, cleaned_text = poster_pipeline(args.image_path)
        print(cleaned_text)
        with open("poster_figure.jpg", "wb") as f:
            f.write(original_figure)
    elif image_category == "book":
        extracted_figures, cleaned_text = book_pipeline(args.image_path)
        print(cleaned_text)
        for index, figure in enumerate(extracted_figures, start=1):
            with open(f"book_figure_{index:02d}.jpg", "wb") as f:
                f.write(figure)
    else:
        original_figure, cleaned_text = other_pipeline(args.image_path)
        print(cleaned_text)
        with open("other_figure.jpg", "wb") as f:
            f.write(original_figure)