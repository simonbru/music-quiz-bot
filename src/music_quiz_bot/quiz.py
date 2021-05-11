from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Sample:
    # TODO: add/modify relevant fields
    track_title: str
    title: str
    image_url: str
    image_data: bytes
    pass



def unescape_js_unicode(match):
    return chr(int(match.group(1), 16))


class MusicQuizSession:
    def __init__(self):
        self.client = httpx.AsyncClient()

    async def get_random_sample(self, require_solution=False):
        ...  # TODO: implement
