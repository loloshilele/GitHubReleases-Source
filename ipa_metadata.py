"""Bounded ZIP range inspection with temporary-download fallback (stdlib)."""
import io
import plistlib
import tempfile
import urllib.request
import zipfile
from urllib.parse import urlsplit
class RangeUnavailable(RuntimeError):
    pass


class RemoteZip(io.RawIOBase):
    """Seekable, bounded HTTP Range reader for zipfile (central directory + plist only)."""
    def __init__(self, url: str, token: str, size: int):
        super().__init__()
        self.url, self.size, self.position = url, size, 0
        from source_catalog import PublicRedirect
        self.opener = urllib.request.build_opener(PublicRedirect())
        self.headers = {'User-Agent': 'GitHubReleases/1.0', 'Accept': 'application/octet-stream'}
        if size <= 0:
            raise RangeUnavailable('附件大小未知')
        self.transferred = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if target < 0:
            raise ValueError('negative seek')
        self.position = target
        return target

    def read(self, count=-1):
        count = min(self.size - self.position, count if count >= 0 else self.size)
        if count <= 0:
            return b''
        if count > 8 * 1024 * 1024 or self.transferred + count > 16 * 1024 * 1024:
            raise RangeUnavailable('ZIP 元数据超出分段读取上限')
        start, end = self.position, self.position + count - 1
        request = urllib.request.Request(self.url, headers={**self.headers, 'Range': f'bytes={start}-{end}'})
        with self.opener.open(request, timeout=40) as response:
            expected = f'bytes {start}-{end}/{self.size}'
            if response.status != 206 or response.headers.get('Content-Range') != expected:
                # A server returning 200 is closed before its full body is consumed.
                raise RangeUnavailable('服务器不支持准确的 HTTP Range')
            data = response.read(count + 1)
        if len(data) != count:
            raise RangeUnavailable('分段响应长度不符')
        self.transferred += len(data)
        self.position += len(data)
        return data



def read_metadata(archive):
    names = [n for n in archive.namelist() if len(n.split('/')) == 3
             and n.startswith('Payload/') and n.endswith('.app/Info.plist')]
    if len(names) != 1:
        raise ValueError('IPA must contain one root application plist')
    info = archive.getinfo(names[0])
    if info.file_size > 2 * 1024 * 1024:
        raise ValueError('IPA plist too large')
    return plistlib.loads(archive.read(info))

def inspect_ipa(url, size):
    from source_catalog import PublicRedirect, public_https
    if not public_https(url) or not isinstance(size, int) or not 0 < size <= 2 * 1024**3:
        raise ValueError('Invalid IPA URL or size')
    try:
        with RemoteZip(url, '', size) as reader, zipfile.ZipFile(reader) as archive:
            metadata = read_metadata(archive)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        opener = urllib.request.build_opener(PublicRedirect())
        with tempfile.TemporaryFile() as target:
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'GitHubReleases-Source'}), timeout=90) as response:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        raise ValueError('IPA exceeds declared size')
                    target.write(chunk)
            if total != size:
                raise ValueError('Incomplete IPA download')
            target.seek(0)
            with zipfile.ZipFile(target) as archive:
                metadata = read_metadata(archive)
    for field in ('CFBundleIdentifier', 'CFBundleShortVersionString'):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError('Missing IPA metadata: ' + field)
    return {key: str(value) for key, value in metadata.items()
            if key in ('CFBundleIdentifier', 'CFBundleShortVersionString', 'CFBundleVersion',
                       'CFBundleDisplayName', 'CFBundleName', 'MinimumOSVersion')}

