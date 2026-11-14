#!/usr/bin/env python3
"""Install the pinned Chinese embedding model used by local memory search."""
from pathlib import Path
from subprocess import run

BASE = 'https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b069aac4d136ba6bc1122f995fedcfd2781/'


def main():
    folder = Path(__file__).resolve().parents[1] / 'data' / 'embedding-model'
    folder.mkdir(parents=True, exist_ok=True)
    for source, name in [('tokenizer.json', 'tokenizer.json'), ('onnx/model_int8.onnx', 'model.onnx')]:
        destination = folder / name
        if not destination.exists():
            pending = destination.with_suffix('.part')
            run(['curl', '-fLsS', '--retry', '3', '--max-time', '120', BASE + source,
                 '-o', str(pending)], check=True)
            pending.replace(destination)
        print(destination, flush=True)


if __name__ == '__main__':
    main()
