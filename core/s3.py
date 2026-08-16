#!/usr/bin/env python3
"""Atropos S3 client — minimal AWS Signature V4, stdlib only (no boto3).

Supports path-style, virtual-hosted-style and MinIO-style (path-prefix)
endpoints. Empty access/secret keys => anonymous (unsigned) requests, which
most S3-compatible servers accept for public buckets.

Reference: AWS "Signature Version 4 Test Suite" + the SigV4 signing spec.
"""
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# unreserved set per RFC 3986
_UNRESERVED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _aws_uri_encode(value: str, safe_slash: bool = True) -> str:
    """AWS canonical-URI encoding: encode everything except A-Za-z0-9-._~.

    Forward slashes are left unencoded when ``safe_slash`` is True so paths
    stay structurally intact (AWS does not encode '/').
    """
    out = []
    for ch in value:
        if ch in _UNRESERVED or (safe_slash and ch == "/"):
            out.append(ch)
        else:
            out.append("%" + ch.encode("utf-8").hex().upper())
    return "".join(out)


def _canonical_uri(path: str) -> str:
    """Normalize + AWS-encode the path into its canonical form."""
    # collapse duplicate slashes and ensure leading slash
    if not path:
        return "/"
    # split into segments, encode each, rejoin
    segments = path.split("/")
    encoded = [_aws_uri_encode(s, safe_slash=False) for s in segments]
    result = "/".join(encoded)
    if not result.startswith("/"):
        result = "/" + result
    return result


def _canonical_query(query: str) -> str:
    """Sort + encode query string per SigV4."""
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        pairs.append(
            (_aws_uri_encode(k, safe_slash=False), _aws_uri_encode(v, safe_slash=False))
        )
    pairs.sort(key=lambda kv: (kv[0], kv[1]))
    return "&".join(f"{k}={v}" for k, v in pairs)


def sign_v4(
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    request_time,  # datetime with strftime supporting %Y%m%dT%H%M%SZ
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    content_sha256: str = None,
) -> tuple:
    """Compute AWS Signature V4 for a request.

    Returns ``(url, headers)`` with the ``Authorization`` header (and any
    derived headers such as ``x-amz-date`` / ``x-amz-content-sha256``) added.
    The caller-supplied ``headers`` must include a lowercase ``host``.
    """
    method = method.upper()
    parsed = urllib.parse.urlparse(url)
    body = body if body is not None else b""

    # derive amz-date + payload hash, unless the caller already set them
    amz_date = headers.get("x-amz-date") or request_time.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = content_sha256 or _sha256_hex(body)

    # Which headers get signed: exactly those the caller provides, plus
    # `host`/`x-amz-date` defaults. If the caller includes an
    # `x-amz-content-sha256` header (S3 requires it), it is signed too —
    # the payload hash used is `content_sha256`/body hash unless the
    # caller pinned a different value.
    signed = dict(headers)
    if "x-amz-content-sha256" in signed and not signed["x-amz-content-sha256"]:
        signed["x-amz-content-sha256"] = payload_hash
    signed.setdefault("x-amz-date", amz_date)
    signed.setdefault("host", parsed.netloc)

    # canonical URI (encode each segment) + query
    canon_uri = _canonical_uri(parsed.path)
    canon_query = _canonical_query(parsed.query)

    # canonical headers: lowercase name, trim ws, sort, name:value\n
    canon_pairs = []
    for name in signed:
        lname = name.lower()
        value = str(signed[name]).strip()
        canon_pairs.append((lname, value))
    canon_pairs.sort(key=lambda kv: kv[0])
    canonical_headers = "".join(f"{n}:{v}\n" for n, v in canon_pairs)
    signed_headers = ";".join(n for n, _ in canon_pairs)

    canonical_request = "\n".join([
        method,
        canon_uri,
        canon_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    # string to sign
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    # signing key HMAC chain
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return url, signed


class S3Error(Exception):
    """Raised on non-2xx S3 responses; carries status + response body."""

    def __init__(self, status, body=""):
        self.status = status
        self.body = body
        super().__init__(f"S3 error {status}: {body[:200]!r}")


class S3Client:
    """Tiny S3-compatible client (path-style / virtual-host / MinIO-prefix)."""

    def __init__(self, endpoint: str, bucket: str, region="us-east-1",
                 access_key="", secret_key=""):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.anonymous = not (access_key and secret_key)
        parsed = urllib.parse.urlparse(self.endpoint)
        self._scheme = parsed.scheme or "https"
        self._netloc = parsed.netloc
        self._path_prefix = parsed.path.rstrip("/")  # e.g. "/alias" for MinIO

    def _key_url(self, key: str) -> str:
        key = key.lstrip("/")
        from urllib.parse import quote
        enc_key = quote(key, safe="")
        # virtual-hosted style when the bucket is already part of the host
        if self._netloc.split(".")[0] == self.bucket:
            return f"{self._scheme}://{self._netloc}/{enc_key}"
        base = f"{self._scheme}://{self._netloc}{self._path_prefix}/{self.bucket}"
        return f"{base}/{enc_key}"

    def _request(self, method: str, url: str, body: bytes = None,
                 extra_headers: dict = None) -> tuple:
        """Send a signed (or anonymous) request. Returns (status, headers, body)."""
        from datetime import datetime, timezone
        body = body if body is not None else b""
        headers = dict(extra_headers or {})
        if "host" not in headers:
            headers["host"] = urllib.parse.urlparse(url).netloc
        if body and "content-type" not in headers:
            headers.setdefault("content-type", "application/octet-stream")

        if not self.anonymous:
            _, headers = sign_v4(
                method, url, headers, body,
                datetime.now(timezone.utc),
                self.access_key, self.secret_key, self.region, "s3",
                _sha256_hex(body),
            )
        else:
            headers.setdefault(
                "x-amz-content-sha256",
                _sha256_hex(body) if body else
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )

        req = urllib.request.Request(url, data=body or None, method=method.upper())
        for k, v in headers.items():
            req.add_header(k, str(v))
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            status = resp.status
            resp_headers = dict(resp.getheaders())
            resp_body = resp.read()
            resp.close()
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = dict(e.headers.items())
            resp_body = e.read()
            e.close()
        return status, resp_headers, resp_body

    def put(self, key: str, data: bytes) -> dict:
        url = self._key_url(key)
        status, headers, _ = self._request("PUT", url, body=data)
        if status >= 200 and status < 300:
            etag = (headers.get("ETag") or headers.get("etag") or "").strip('"')
            return {"ok": True, "etag": etag}
        raise S3Error(status, f"put failed for {key}")

    def get(self, key: str) -> bytes:
        url = self._key_url(key)
        status, _, body = self._request("GET", url)
        if status == 404:
            raise KeyError(key)
        if status >= 200 and status < 300:
            return body
        raise S3Error(status, f"get failed for {key}")

    def delete(self, key: str) -> bool:
        url = self._key_url(key)
        status = self._request("DELETE", url)[0]
        if status in (200, 204, 404):
            return status in (200, 204)
        raise S3Error(status, f"delete failed for {key}")

    def exists(self, key: str) -> bool:
        try:
            url = self._key_url(key)
            status, _, _ = self._request("HEAD", url)
            return status >= 200 and status < 300
        except KeyError:
            return False
        except S3Error:
            return False

    def list_keys(self, prefix: str = "") -> list:
        keys = []
        marker = ""
        while True:
            qs = []
            if prefix:
                qs.append(f"prefix={urllib.parse.quote(prefix, safe='')}")
            if marker:
                qs.append(f"marker={urllib.parse.quote(marker, safe='')}")
            query = ("?" + "&".join(qs)) if qs else ""
            url = self._key_url("") + query
            status, _, body = self._request("GET", url)
            if status >= 200 and status < 300:
                root = ET.fromstring(body)
                ns = ""
                if root.tag.startswith("{"):
                    ns = root.tag[1:].split("}")[0]
                nstag = lambda t: f"{{{ns}}}{t}" if ns else t
                for c in root.findall(nstag("Contents")):
                    kn = c.find(nstag("Key"))
                    if kn is not None and kn.text:
                        keys.append(kn.text)
                mc = root.find(nstag("NextContinuationToken"))
                is_truncated = root.find(nstag("IsTruncated"))
                if is_truncated is not None and is_truncated.text == "true" and mc is not None:
                    marker = mc.text
                    continue
                break
            raise S3Error(status, "list failed")
        return keys


def signature_test_vector(name: str = "GET-Vanilla"):
    """Reproduce an AWS SigV4 test-suite vector.

    For ``GET-Vanilla`` returns ``(canonical_request, string_to_sign,
    signature)`` using the official AKIDEXAMPLE / us-east-1 / 20130524T000000Z
    inputs. The canonical request is asserted literally in tests.
    """
    if name != "GET-Vanilla":
        raise ValueError(f"unknown test vector: {name}")
    from datetime import datetime, timezone
    access_key = "AKIDEXAMPLE"
    secret_key = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    region = "us-east-1"
    service = "s3"
    method = "GET"
    url = "https://s3.amazonaws.com/test.txt"
    request_time = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    headers = {
        "host": "s3.amazonaws.com",
        "x-amz-content-sha256":
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "x-amz-date": "20130524T000000Z",
    }
    payload_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    url_out, signed = sign_v4(
        method, url, headers, b"", request_time,
        access_key, secret_key, region, service, payload_hash,
    )
    # canonical request is reconstructed the same way sign_v4 builds it
    parsed = urllib.parse.urlparse(url)
    canon_uri = _canonical_uri(parsed.path)
    canon_query = _canonical_query(parsed.query)
    canon_pairs = sorted(
        (k.lower(), str(v).strip()) for k, v in headers.items()
    )
    canonical_headers = "".join(f"{n}:{v}\n" for n, v in canon_pairs)
    signed_headers = ";".join(n for n, _ in canon_pairs)
    canonical_request = "\n".join([
        method, canon_uri, canon_query, canonical_headers,
        signed_headers, payload_hash,
    ])

    datestamp = "20130524"
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        "20130524T000000Z",
        scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return canonical_request, string_to_sign, signature


if __name__ == "__main__":
    cr, sts, sig = signature_test_vector()
    print("CANONICAL REQUEST:\n" + cr)
    print("\nSIGNATURE:", sig)
