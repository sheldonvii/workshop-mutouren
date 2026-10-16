"""下载姿态模型（n、s、m 三个尺寸，默认使用 m）并校验 SHA-256。已有文件校验不符时报错，不覆盖。

用法：.venv/bin/python scripts/download_models.py [--verify-only]
"""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
ZOO = 'https://archive.spacemit.com/spacemit-ai/model_zoo'
MODELS = {
    'yolov8n-pose.q.onnx': (f'{ZOO}/vision/yolov8_pose/yolov8n-pose.q.onnx',
                            '7a46bf4e4739548d9954e69ea21e24df70ef25d47d23fd70de2bebe4e55d511c'),
    'yolov8s-pose.q.onnx': (f'{ZOO}/vision/yolov8_pose/yolov8s-pose.q.onnx',
                            '523ac195f0866e8a53385c0ff012982d375ac6a1e1b28b0ce921ebf5005192f1'),
    'yolov8m-pose.q.onnx': (f'{ZOO}/vision/yolov8_pose/yolov8m-pose.q.onnx',
                            'e9bf958bd2b6dc1926eeba1a6815ab6b34fc804c07d7f558818f1e312c997368'),
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--verify-only', action='store_true', help='只校验已有文件，不下载')
    args = parser.parse_args()
    folder = ROOT / 'models'
    if not args.verify_only:
        folder.mkdir(exist_ok=True)
    for name, (url, expected) in MODELS.items():
        target = folder / name
        if target.exists():
            if digest(target) != expected:
                raise RuntimeError(f'{name}：校验值不符，已有文件未被替换')
        elif args.verify_only:
            raise FileNotFoundError(target)
        else:
            temporary = target.with_suffix('.onnx.part')
            try:
                with urlopen(url, timeout=120) as response, temporary.open('wb') as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if digest(temporary) != expected:
                    raise RuntimeError(f'{name}：下载文件校验值不符')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        print(name, '校验通过')


if __name__ == '__main__':
    main()
