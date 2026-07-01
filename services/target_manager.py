from dataclasses import dataclass

import cv2


@dataclass
class TargetBox:
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def center(self):
        return (self.x_min + self.x_max) / 2, (self.y_min + self.y_max) / 2

    @property
    def height(self):
        return self.y_max - self.y_min


class TargetManager:
    def get_group_target(self, results):
        boxes_obj = results[0].boxes
        if boxes_obj.id is None or len(boxes_obj.id) == 0:
            return None

        boxes = boxes_obj.xyxy.cpu().numpy()
        return TargetBox(
            x_min=min(b[0] for b in boxes),
            y_min=min(b[1] for b in boxes),
            x_max=max(b[2] for b in boxes),
            y_max=max(b[3] for b in boxes),
        )

    def annotate(self, frame, target):
        cv2.rectangle(
            frame,
            (int(target.x_min), int(target.y_min)),
            (int(target.x_max), int(target.y_max)),
            (0, 255, 255),
            3,
        )
        cv2.putText(
            frame,
            "TARGET LOCKED",
            (int(target.x_min), int(target.y_min) - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 0, 255),
            2,
        )
