"""Avatar media handling."""


def upload_avatar(file, scope, meta=None):
    """Push an avatar image into storage and return its locator."""
    key = build_key(scope, file.filename)
    object_store.put(key, file.content, metadata=meta)
    return {"locator": key, "scope": scope}


def normalize_image(img, dims):
    """Bring an image to a canonical size/format and drop EXIF data."""
    scaled = scale(img, dims)
    converted = transcode(scaled, "png")
    return clear_exif(converted)


def save_avatar_variant(img, scope, dims):
    """Validate an avatar image, then store a resized variant."""
    if not valid_image(img):
        raise ValueError("invalid avatar image")
    normalized = normalize_image(img, dims)
    return upload_avatar(normalized, scope, meta={"variant": dims})
