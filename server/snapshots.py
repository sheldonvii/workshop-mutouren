"""淘汰抓拍：保存触发判定的那一帧，画出骨架并标红动了的部位。只保留最近若干张。"""
import re
import time
from pathlib import Path

import cv2

from .vision import PARTS, SKELETON

_ID = re.compile(r'^[0-9a-z-]{1,64}$')

# 与页面 web/js/draw.js 的配色一致（BGR）：玩家为号码布白，其他人半透明，动了的部位为守卫红
_BIB = (241, 246, 245)
_RED = (94, 61, 226)


def _skeleton(image, person, color, width, hot, vis_min):
    k = person.kps
    hot_width = round(width * 1.6)
    for a, b in SKELETON:
        if k[a, 2] >= vis_min and k[b, 2] >= vis_min:
            is_hot = a in hot and b in hot
            cv2.line(image, (int(k[a, 0]), int(k[a, 1])), (int(k[b, 0]), int(k[b, 1])),
                     _RED if is_hot else color, hot_width if is_hot else width, cv2.LINE_AA)
    for i, (x, y, v) in enumerate(k):
        if v >= vis_min:
            radius = int(width * (1.4 if i in hot else 0.9)) + 1
            cv2.circle(image, (int(x), int(y)), radius, _RED if i in hot else color, -1, cv2.LINE_AA)


def draw_people(image, people, player=None, part=None, vis_min=0.5):
    unit = max(2, image.shape[0] // 120)
    others = image.copy()
    for person in people:
        if person is not player:
            _skeleton(others, person, _BIB, max(1, unit // 2), set(), vis_min)
    cv2.addWeighted(others, 0.45, image, 0.55, 0, dst=image)
    if player is not None:
        _skeleton(image, player, _BIB, unit, set(PARTS.get(part, ())), vis_min)
    return image


class SnapshotStore:
    def __init__(self, folder, keep=20):
        self.folder = Path(folder)
        self.keep = keep

    def save(self, image, people, player, part, run_id, round_no):
        self.folder.mkdir(parents=True, exist_ok=True)
        snap_id = f'{run_id or "norun"}-{round_no}-{int(time.time() * 1000)}'
        canvas = draw_people(image.copy(), people, player, part)
        cv2.imwrite(str(self.folder / f'{snap_id}.jpg'), canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
        for old in sorted(self.folder.glob('*.jpg'), key=lambda p: p.stat().st_mtime)[:-self.keep]:
            old.unlink(missing_ok=True)
        return snap_id

    def path(self, snap_id):
        if not _ID.match(snap_id):
            return None
        path = self.folder / f'{snap_id}.jpg'
        return path if path.is_file() else None
