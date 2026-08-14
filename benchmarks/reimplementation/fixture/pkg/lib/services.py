"""Shared infrastructure services."""


class StorageService:
    def upload(self, payload, namespace, metadata=None):
        """Write a blob to object storage and return its handle."""
        key = self._make_key(namespace, payload.name)
        self.client.put_object(key, payload.bytes, metadata=metadata)
        return {"key": key, "namespace": namespace}


class ExportService:
    def create_bundle(self, root_dir, destination):
        """Zip a directory tree and attach a checksum for the archive."""
        archive = self._zip(root_dir)
        checksum = self._digest(archive)
        destination.write_bytes(archive)
        return {"archive": str(destination), "sha256": checksum}


class ConfigLoader:
    def load_from_url(self, url, cache):
        """Fetch and parse a remote JSON config, caching the parsed value."""
        raw = self.http.get(url)
        parsed = self.parser.parse(raw)
        cache.set(url, parsed)
        return parsed


class ImageProcessor:
    def prepare(self, image, size):
        """Resize, normalize format, and strip metadata from an image."""
        resized = self._resize(image, size)
        normalized = self._convert(resized, "png")
        return self._strip_metadata(normalized)
