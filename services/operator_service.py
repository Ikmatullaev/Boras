class OperatorService:
    """Manual operator commands: move/zoom/focus + guard toggle.

    Lights functionality was removed — the camera has its own light sensors
    and manages IR/White light automatically.
    """

    def __init__(self, runtime, ptz, logger):
        self.runtime = runtime
        self.ptz = ptz
        self.logger = logger

    def move(self, direction):
        commands = {
            "up": lambda: self.ptz.move(0, 0.5),
            "down": lambda: self.ptz.move(0, -0.5),
            "left": lambda: self.ptz.move(-0.5, 0),
            "right": lambda: self.ptz.move(0.5, 0),
            "stop": self.ptz.stop_pantilt,
        }
        return self._run_manual_command("move", direction, commands)

    def zoom(self, direction):
        commands = {
            "in": lambda: self.ptz.zoom(0.3),
            "out": lambda: self.ptz.zoom(-0.3),
            "stop": self.ptz.stop_zoom,
        }
        return self._run_manual_command("zoom", direction, commands)

    def focus(self, direction):
        commands = {
            "near": lambda: self.ptz.focus(0.3),
            "far": lambda: self.ptz.focus(-0.3),
            "stop": self.ptz.stop_focus,
        }
        return self._run_manual_command("focus", direction, commands)

    def toggle_guard(self):
        return {"status": self.runtime.toggle_guard()}

    def _run_manual_command(self, command_type, direction, commands):
        command = commands.get(direction)
        if command is None:
            return {"status": "error", "message": f"Unknown direction: {direction}"}

        self.runtime.manual_override()
        command()
        return {"status": "success", "direction": direction}
