"""Small, on-demand previews from Amazon's public image hosts."""
from io import BytesIO
import queue
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = {"m.media-amazon.com", "images-na.ssl-images-amazon.com",
         "images-eu.ssl-images-amazon.com", "images.amazon.com"}
MAX_BYTES = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def is_cover_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.hostname in HOSTS and url.port in (None, 443)
                and not (url.username or url.password or url.fragment) and url.path.startswith("/images/"))
    except ValueError:
        return False


def load_cover(url):
    from PIL import Image
    if not is_cover_url(url):
        return None
    opener = build_opener(NoRedirect())
    with opener.open(Request(url, headers={"Accept": "image/*"}), timeout=6) as response:
        if not response.headers.get_content_type().startswith("image/"):
            return None
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return None
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > 4_000_000:
            return None
        image.thumbnail((168, 168))
        return image.convert("RGB")


class CoverLoader:
    def __init__(self):
        self.requests = queue.Queue(maxsize=1)
        self.results = queue.SimpleQueue()
        self.closed = threading.Event()
        self.started = False

    def request(self, url):
        if not url or not is_cover_url(url) or self.closed.is_set():
            return
        if not self.started:
            self.started = True
            threading.Thread(target=self._run, daemon=True, name="hoerwerk-cover").start()
        try:
            self.requests.get_nowait()
        except queue.Empty:
            pass
        self.requests.put_nowait(url)

    def _run(self):
        cache = {}
        while not self.closed.is_set():
            try:
                url = self.requests.get(timeout=.25)
            except queue.Empty:
                continue
            if url not in cache:
                try:
                    image = load_cover(url)
                except Exception:
                    image = None
                if image is not None:
                    if len(cache) >= 24:
                        del cache[next(iter(cache))]
                    cache[url] = image
            self.results.put((url, cache.get(url)))
