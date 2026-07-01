"""Юнит-тесты для TargetManager."""
import numpy as np
import pytest

from services.target_manager import TargetBox, TargetManager
from conftest import FakeResult


class TestTargetBox:
    def test_center_single_box(self):
        box = TargetBox(x_min=100, y_min=200, x_max=300, y_max=400)
        assert box.center == (200.0, 300.0)

    def test_height_single_box(self):
        box = TargetBox(x_min=100, y_min=200, x_max=300, y_max=400)
        assert box.height == 200.0

    def test_center_zero_size(self):
        box = TargetBox(x_min=50, y_min=50, x_max=50, y_max=50)
        assert box.center == (50.0, 50.0)
        assert box.height == 0.0


class TestGetGroupTarget:
    def test_empty_results_id_none_returns_none(self):
        """Если YOLO ничего не нашёл (id is None) — должен вернуть None."""
        mgr = TargetManager()
        result = FakeResult()  # boxes.id = None по умолчанию
        assert mgr.get_group_target([result]) is None

    def test_empty_results_zero_boxes_returns_none(self):
        """Если список боксов пустой — должен вернуть None."""
        mgr = TargetManager()
        result = FakeResult(boxes_xyxy=[], ids=[])
        assert mgr.get_group_target([result]) is None

    def test_single_box_returns_that_box(self):
        mgr = TargetManager()
        result = FakeResult(boxes_xyxy=[[100, 200, 300, 400]], ids=[1])
        target = mgr.get_group_target([result])
        assert target is not None
        assert target.x_min == 100
        assert target.y_min == 200
        assert target.x_max == 300
        assert target.y_max == 400

    def test_multiple_boxes_returns_group_bounding_box(self):
        """Групповой таргет — это outer bounding box всех боксов.
        x_min = min всех x_min, x_max = max всех x_max, и т.д."""
        mgr = TargetManager()
        result = FakeResult(
            boxes_xyxy=[
                [100, 100, 200, 200],   # левый верхний
                [500, 300, 700, 500],   # правый нижний
                [300, 150, 400, 250],   # центр
            ],
            ids=[1, 2, 3],
        )
        target = mgr.get_group_target([result])
        assert target is not None
        assert target.x_min == 100
        assert target.y_min == 100
        assert target.x_max == 700
        assert target.y_max == 500
        # center — центр группового бокса
        assert target.center == (400.0, 300.0)
        # height — высота группового бокса
        assert target.height == 400.0

    def test_results_list_accesses_first_element(self):
        """Должен брать results[0] — проверяем, что индексация корректна."""
        mgr = TargetManager()
        result = FakeResult(boxes_xyxy=[[10, 10, 20, 20]], ids=[1])
        # Передаём список из одного элемента
        target = mgr.get_group_target([result])
        assert target is not None
        assert target.x_min == 10


class TestAnnotate:
    def test_annotate_draws_rectangle_without_error(self, fake_frame):
        """annotate() использует cv2.rectangle и cv2.putText — должен отработать
        без исключения. cv2 в тестах подменён stub'ом, поэтому просто проверяем,
        что вызов не падает."""
        mgr = TargetManager()
        target = TargetBox(x_min=100, y_min=100, x_max=300, y_max=400)
        # Не должно выбросить исключение
        mgr.annotate(fake_frame, target)
