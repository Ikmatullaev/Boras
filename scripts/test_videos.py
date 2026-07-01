#!/usr/bin/env python3
"""
Boras — Video Batch Testing Tool

Прогоняет все .mp4 файлы из папки test_videos/ через YOLOv8 и сохраняет:
  1. Аннотированные видео (с зелёными рамками) → test_videos/annotated/
  2. CSV отчёт со статистикой → test_videos/results.csv
  3. JSON с детальной статистикой → test_videos/results.json
  4. Summary таблицу в терминал

Использование:
    python3 scripts/test_videos.py
    python3 scripts/test_videos.py --videos /path/to/videos --model yolov8n.pt
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_args():
    p = argparse.ArgumentParser(
        description="Boras video batch testing tool — runs YOLO on all .mp4 files"
    )
    p.add_argument("--videos", default="test_videos",
                   help="Папка с входными видео (default: test_videos)")
    p.add_argument("--model", default="yolov8n.pt",
                   help="YOLO model (yolov8n.pt/s/m/l/x). Default: yolov8n.pt")
    p.add_argument("--conf", type=float, default=0.5,
                   help="Min detection confidence threshold (default: 0.5)")
    p.add_argument("--classes", type=int, nargs="+", default=[0],
                   help="COCO class IDs to detect (default: 0 = person)")
    p.add_argument("--no-annotate", action="store_true",
                   help="Не сохранять аннотированные видео (только статистика)")
    p.add_argument("--verbose", action="store_true",
                   help="Подробный вывод (печатать прогресс каждого кадра)")
    return p.parse_args()


def find_video_files(videos_dir: Path):
    if not videos_dir.exists():
        return []
    extensions = {".mp4", ".avi", ".mov", ".mkv"}
    return [f for f in sorted(videos_dir.iterdir())
            if f.is_file() and f.suffix.lower() in extensions]


def process_video(video_path, model, args, annotated_dir):
    """Прогоняет одно видео через YOLO, возвращает статистику."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return {"error": "cannot_open", "video_file": video_path.name,
                "total_frames": 0, "frames_with_detection": 0,
                "detection_rate_pct": 0, "avg_confidence": 0,
                "max_people_in_frame": 0, "total_detections": 0,
                "video_fps": 0, "duration_sec": 0, "resolution": "unknown",
                "processing_fps": 0, "processing_time_sec": 0}

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    writer = None
    if not args.no_annotate:
        annotated_path = annotated_dir / f"annotated_{video_path.name}"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(annotated_path), fourcc, fps, (width, height))

    frames_with_detection = 0
    confidences = []
    max_people_in_frame = 0
    total_people_detected = 0
    frame_count = 0

    start_time = time.monotonic()

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_count += 1

        results = model.predict(frame, conf=args.conf, classes=args.classes, verbose=False)
        boxes = results[0].boxes

        # ИСПРАВЛЕНО: используем len(boxes) напрямую, без проверки boxes.id
        # predict() не назначает id (это атрибут track()), но boxes существуют
        num_people = len(boxes) if boxes is not None else 0

        if num_people > 0:
            frames_with_detection += 1
            # Извлекаем confidences — у predict() boxes.conf всегда есть если есть boxes
            if boxes is not None and hasattr(boxes, 'conf') and boxes.conf is not None:
                confs = boxes.conf.cpu().numpy()
                if len(confs) > 0:
                    confidences.extend(confs.tolist())
                    total_people_detected += num_people
            max_people_in_frame = max(max_people_in_frame, num_people)

        if writer is not None:
            annotated = results[0].plot()
            writer.write(annotated)

        if args.verbose and frame_count % 30 == 0:
            pct = (frame_count / total_frames * 100) if total_frames else 0
            print(f"    frame {frame_count}/{total_frames} ({pct:.0f}%) — people: {num_people}")

    processing_time = time.monotonic() - start_time
    cap.release()
    if writer is not None:
        writer.release()

    detection_rate = (frames_with_detection / frame_count * 100) if frame_count else 0
    avg_conf = (sum(confidences) / len(confidences)) if confidences else 0
    processing_fps = (frame_count / processing_time) if processing_time > 0 else 0
    duration_sec = (frame_count / fps) if fps else 0

    return {
        "video_file": video_path.name,
        "total_frames": frame_count,
        "frames_with_detection": frames_with_detection,
        "detection_rate_pct": round(detection_rate, 1),
        "avg_confidence": round(avg_conf, 3),
        "max_people_in_frame": max_people_in_frame,
        "total_detections": total_people_detected,
        "video_fps": round(fps, 1),
        "duration_sec": round(duration_sec, 1),
        "resolution": f"{width}x{height}",
        "processing_fps": round(processing_fps, 1),
        "processing_time_sec": round(processing_time, 1),
        "error": None,
    }


def write_csv(results, csv_path):
    if not results:
        return
    fields = ["video_file", "total_frames", "frames_with_detection",
              "detection_rate_pct", "avg_confidence", "max_people_in_frame",
              "total_detections", "video_fps", "duration_sec", "resolution",
              "processing_fps", "processing_time_sec", "error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(r)


def write_json(results, json_path, args):
    summary = {
        "test_date": datetime.now().isoformat(),
        "model": args.model,
        "confidence_threshold": args.conf,
        "classes": args.classes,
        "total_videos": len(results),
        "videos": results,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def print_summary(results):
    if not results:
        print("\nНет результатов — нет видео для тестирования.")
        return

    print("\n" + "=" * 100)
    print(f"{'VIDEO':<30} {'FRAMES':>8} {'DETECTED':>10} {'RATE':>8} "
          f"{'AVG_CONF':>10} {'MAX_PEOPLE':>12} {'PROC_FPS':>10}")
    print("=" * 100)

    total_frames = 0
    total_detected = 0
    all_confs = []

    for r in results:
        if r.get("error"):
            print(f"  {r['video_file']:<28} ERROR: {r['error']}")
            continue
        print(f"  {r['video_file']:<28} {r['total_frames']:>8} "
              f"{r['frames_with_detection']:>10} {r['detection_rate_pct']:>7}% "
              f"{r['avg_confidence']:>10} {r['max_people_in_frame']:>12} "
              f"{r['processing_fps']:>10}")
        total_frames += r["total_frames"]
        total_detected += r["frames_with_detection"]
        if r["avg_confidence"] > 0:
            all_confs.append(r["avg_confidence"])

    print("=" * 100)
    overall_rate = (total_detected / total_frames * 100) if total_frames else 0
    overall_conf = (sum(all_confs) / len(all_confs)) if all_confs else 0
    print(f"  {'TOTAL':<28} {total_frames:>8} {total_detected:>10} "
          f"{overall_rate:>7.1f}% {overall_conf:>10}")
    print()


def main():
    args = parse_args()

    videos_dir = ROOT / args.videos
    annotated_dir = videos_dir / "annotated"
    csv_path = videos_dir / "results.csv"
    json_path = videos_dir / "results.json"

    videos_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_annotate:
        annotated_dir.mkdir(parents=True, exist_ok=True)

    video_files = find_video_files(videos_dir)
    if not video_files:
        print(f"\nНе найдено видео файлов в: {videos_dir}")
        print(f"Положи .mp4 файлы в эту папку и запусти снова.")
        return 1

    print(f"\nНайдено {len(video_files)} видео файлов")
    print(f"Папка: {videos_dir}")
    print(f"Модель: {args.model}")
    print(f"Минимальная уверенность: {args.conf}")
    print(f"Классы: {args.classes} (0 = person)")
    print()

    print("Загружаю YOLO модель...")
    try:
        from ultralytics import YOLO
        model = YOLO(args.model)
    except ImportError:
        print("ultralytics не установлен. Выполни: pip install ultralytics")
        return 1
    except Exception as e:
        print(f"Не удалось загрузить модель: {e}")
        return 1

    print(f"Модель загружена\n")

    results = []
    for i, vf in enumerate(video_files, 1):
        print(f"[{i}/{len(video_files)}] Обрабатываю: {vf.name}")
        try:
            r = process_video(vf, model, args, annotated_dir)
            if r.get("error"):
                print(f"  Ошибка: {r['error']}")
            else:
                print(f"  Готово: {r['frames_with_detection']}/{r['total_frames']} "
                      f"кадров с обнаружением ({r['detection_rate_pct']}%), "
                      f"avg conf={r['avg_confidence']}, "
                      f"{r['processing_fps']} FPS")
            results.append(r)
        except Exception as e:
            print(f"  Исключение: {e}")
            results.append({
                "video_file": vf.name, "error": str(e),
                "total_frames": 0, "frames_with_detection": 0,
                "detection_rate_pct": 0, "avg_confidence": 0,
                "max_people_in_frame": 0, "total_detections": 0,
                "video_fps": 0, "duration_sec": 0, "resolution": "unknown",
                "processing_fps": 0, "processing_time_sec": 0,
            })

    write_csv(results, csv_path)
    write_json(results, json_path, args)
    print_summary(results)

    print(f"CSV отчёт: {csv_path}")
    print(f"JSON отчёт: {json_path}")
    if not args.no_annotate:
        print(f"Аннотированные видео: {annotated_dir}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
