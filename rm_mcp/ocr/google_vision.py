"""
Google Cloud Vision OCR backend.

Supports two authentication methods:
1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
"""

import os
from pathlib import Path
from typing import List, Optional

from rm_mcp.extract.render import _render_rm_to_ocr_png


def _ocr_google_vision(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision API.
    Best quality for handwriting recognition.

    Supports two authentication methods:
    1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
    2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
    """
    api_key = os.environ.get("GOOGLE_VISION_API_KEY")

    if api_key:
        # Use REST API with API key (simpler, no SDK needed)
        return _ocr_google_vision_rest(rm_files, api_key)
    else:
        # Use SDK with service account credentials
        return _ocr_google_vision_sdk(rm_files)


def _ocr_google_vision_rest(rm_files: List[Path], api_key: str) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision REST API with API key.
    """
    import base64

    import requests

    from rm_mcp.ocr.tesseract import _ocr_tesseract

    ocr_results = []

    for rm_file in rm_files:
        tmp_png_path = None
        try:
            tmp_png_path = _render_rm_to_ocr_png(rm_file)
            if tmp_png_path is None:
                continue

            # Read and encode image
            with open(tmp_png_path, "rb") as f:
                image_content = base64.b64encode(f.read()).decode("utf-8")

            # Call Google Vision REST API
            url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
            payload = {
                "requests": [
                    {
                        "image": {"content": image_content},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            }

            response = requests.post(url, json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                if "responses" in data and data["responses"]:
                    resp = data["responses"][0]
                    if "fullTextAnnotation" in resp:
                        text = resp["fullTextAnnotation"]["text"]
                        if text.strip():
                            ocr_results.append(text.strip())
            elif response.status_code in (401, 403):
                # API key invalid or API not enabled - fall back to Tesseract
                return _ocr_tesseract(rm_files)

        except FileNotFoundError:
            return None
        except Exception:
            pass
        finally:
            if tmp_png_path:
                tmp_png_path.unlink(missing_ok=True)

    return ocr_results if ocr_results else None


def _ocr_google_vision_sdk(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision SDK with service account credentials.
    """
    from rm_mcp.ocr.tesseract import _ocr_tesseract

    try:
        from google.cloud import vision

        client = vision.ImageAnnotatorClient()
        ocr_results = []

        for rm_file in rm_files:
            tmp_png_path = None
            try:
                tmp_png_path = _render_rm_to_ocr_png(rm_file)
                if tmp_png_path is None:
                    continue

                # Send to Google Vision API
                with open(tmp_png_path, "rb") as f:
                    content = f.read()

                image = vision.Image(content=content)
                response = client.document_text_detection(image=image)

                if response.error.message:
                    continue

                if response.full_text_annotation.text:
                    ocr_results.append(response.full_text_annotation.text.strip())

            except FileNotFoundError:
                return None
            except Exception:
                pass
            finally:
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # google-cloud-vision not installed, fall back to tesseract
        return _ocr_tesseract(rm_files)
    except Exception:
        # API error, fall back to tesseract
        return _ocr_tesseract(rm_files)
