from __future__ import annotations

import io

from PIL import Image, ImageOps


# EXIF tag 0x0112. 1 means "already upright"; anything else needs a transform.
ORIENTATION_TAG = 0x0112
UPRIGHT_ORIENTATIONS = (None, 0, 1)

# Frappe re-encodes JPEGs at Pillow's default quality (75) while stripping EXIF, so
# re-save at a higher quality here rather than compounding that loss.
JPEG_QUALITY = 90
WEBP_QUALITY = 90


def normalize_image_orientation(content: bytes | None) -> bytes | None:
	"""Bake the EXIF Orientation tag into the pixels before the tag gets dropped.

	Frappe re-saves uploaded JPEGs with ``exif=b""`` when the site has
	``strip_exif_metadata_from_uploaded_images`` enabled (see ``strip_exif_data`` in
	frappe/utils/image.py, called from frappe/core/doctype/file/file.py). That removes
	the Orientation tag without rotating the pixels, so a phone photo ends up stored
	permanently sideways. Rotating here makes the tag redundant by the time Frappe
	discards it, and leaves the privacy-motivated stripping itself intact.

	Returns the original bytes untouched - never a re-encode - for non-images, images
	that are already upright, multi-frame images, and anything Pillow cannot read.
	Never raises: a failed rotation must not block an upload.
	"""
	if not content:
		return content

	try:
		image = Image.open(io.BytesIO(content))
		image_format = (image.format or "").upper()
		if not image_format:
			return content

		# Animated/multi-frame files are out of scope; re-encoding one would drop frames.
		if getattr(image, "n_frames", 1) > 1:
			return content

		if image.getexif().get(ORIENTATION_TAG) in UPRIGHT_ORIENTATIONS:
			return content

		# Read the encoder options off the source, before transpose() hands back an
		# image with no .format and no JPEG layer info.
		save_kwargs = _save_kwargs(image, image_format)

		transposed = ImageOps.exif_transpose(image)
		if transposed is None:
			return content

		# exif_transpose rewrites info["exif"] with the Orientation tag removed, so
		# carrying it over keeps the remaining metadata without re-asserting rotation.
		exif_bytes = transposed.info.get("exif")
		if exif_bytes:
			save_kwargs["exif"] = exif_bytes

		output = io.BytesIO()
		transposed.save(output, format=image_format, **save_kwargs)
		return output.getvalue()
	except Exception:
		# Corrupt file, unsupported codec, encoder rejecting our kwargs - the upload
		# still has to go through, just with the bytes the client sent.
		return content


def _save_kwargs(image: Image.Image, image_format: str) -> dict:
	"""Encoder options that keep the re-save as close to the original as we can get."""
	kwargs: dict = {}

	icc_profile = image.info.get("icc_profile")
	if icc_profile:
		kwargs["icc_profile"] = icc_profile

	if image_format == "JPEG":
		kwargs["quality"] = JPEG_QUALITY
		if image.info.get("progressive"):
			kwargs["progressive"] = True
		sampling = _jpeg_subsampling(image)
		if sampling is not None:
			kwargs["subsampling"] = sampling
	elif image_format == "WEBP":
		kwargs["quality"] = WEBP_QUALITY
		if image.info.get("lossless"):
			kwargs["lossless"] = True

	return kwargs


def _jpeg_subsampling(image: Image.Image) -> int | None:
	"""Chroma subsampling of the source JPEG, so the re-save does not silently change it."""
	try:
		from PIL.JpegImagePlugin import get_sampling

		sampling = get_sampling(image)
	except Exception:
		return None

	# get_sampling returns -1 for layouts that cannot be expressed as a save option.
	return sampling if sampling in (0, 1, 2) else None
