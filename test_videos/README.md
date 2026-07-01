# Test Videos

Положи сюда .mp4 файлы для тестирования YOLO модели.

## Запуск тестов

```bash
python3 scripts/test_videos.py
```

## Что скрипт делает

1. Находит все `.mp4`/`.avi`/`.mov` файлы в этой папке
2. Прогоняет каждый через YOLOv8
3. Сохраняет:
   - `annotated/annotated_<name>.mp4` — видео с нарисованными рамками
   - `results.csv` — таблица со статистикой по каждому видео
   - `results.json` — детальный отчёт
4. Печатает summary таблицу в терминал

## Опции

```bash
# Использовать другую модель (более точную)
python3 scripts/test_videos.py --model yolov8s.pt

# Изменить порог уверенности
python3 scripts/test_videos.py --conf 0.3

# Не сохранять аннотированные видео (только статистика)
python3 scripts/test_videos.py --no-annotate

# Подробный вывод
python3 scripts/test_videos.py --verbose
```

## Метрики в отчёте

| Поле | Описание |
|---|---|
| `total_frames` | Всего кадров в видео |
| `frames_with_detection` | Кадров где YOLO нашёл человека |
| `detection_rate_pct` | % кадров с обнаружением |
| `avg_confidence` | Средняя уверенность модели (0-1) |
| `max_people_in_frame` | Макс людей одновременно в кадре |
| `processing_fps` | Скорость обработки (FPS) |
| `duration_sec` | Длительность видео |
