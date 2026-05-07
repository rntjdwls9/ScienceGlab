"""Storage abstraction layer.

LOCAL backend: filesystem (UPLOAD_DIR).
R2 backend: Cloudflare R2 (S3-compatible).

Switch via STORAGE_BACKEND env var: "local" (default) or "r2".
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from flask import send_from_directory, redirect


class LocalStorage:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(self, data: bytes, name: str) -> int:
        path = self.base_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    def save_filestorage(self, fs, name: str) -> int:
        path = self.base_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fs.save(path)
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def delete(self, name: str) -> None:
        try:
            (self.base_dir / name).unlink(missing_ok=True)
        except OSError:
            pass

    def serve(self, name: str, as_attachment: bool = False,
              download_name: str | None = None):
        return send_from_directory(
            self.base_dir, name,
            as_attachment=as_attachment,
            download_name=download_name,
        )


class R2Storage:
    def __init__(self, account_id: str, access_key: str,
                 secret_key: str, bucket: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )

    def save_bytes(self, data: bytes, name: str) -> int:
        self.client.put_object(Bucket=self.bucket, Key=name, Body=data)
        return len(data)

    def save_filestorage(self, fs, name: str) -> int:
        fs.stream.seek(0)
        data = fs.stream.read()
        self.client.put_object(Bucket=self.bucket, Key=name, Body=data)
        return len(data)

    def delete(self, name: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=name)
        except Exception:
            pass

    def serve(self, name: str, as_attachment: bool = False,
              download_name: str | None = None):
        params = {"Bucket": self.bucket, "Key": name}
        if as_attachment:
            if download_name:
                encoded = quote(download_name)
                params["ResponseContentDisposition"] = (
                    f"attachment; filename*=UTF-8''{encoded}"
                )
            else:
                params["ResponseContentDisposition"] = "attachment"
        url = self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=3600
        )
        return redirect(url)


def get_storage(local_base_dir: Path):
    backend = os.environ.get("STORAGE_BACKEND", "local").lower()
    if backend == "r2":
        return R2Storage(
            account_id=os.environ["R2_ACCOUNT_ID"],
            access_key=os.environ["R2_ACCESS_KEY_ID"],
            secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
        )
    return LocalStorage(local_base_dir)
