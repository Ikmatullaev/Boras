import base64
import hashlib
import logging
import os
import math
import time
from datetime import datetime, timezone

import requests

from config import settings

logger = logging.getLogger("crane.control")


class CranePTZ:
    """
    ONVIF PTZ + Imaging control for the Crane Vision camera.
    """

    def __init__(self, ip, username, password,
                 profile=None, video_source=None, min_command_interval=None,
                 events=None, metrics=None, trace=None, config=None):
        cfg = config or settings.ptz
        http_port = settings.camera.http_port
        self.ptz_url = f"http://{ip}:{http_port}/onvif/ptz_service"
        self.imaging_url = f"http://{ip}:{http_port}/onvif/imaging_service"

        self.username = username
        self.password = password
        self.profile = profile if profile is not None else cfg.profile
        self.video_source = video_source if video_source is not None else cfg.video_source
        self.headers = {'Content-Type': 'application/soap+xml; charset=utf-8'}
        self._http_timeout = cfg.http_timeout

        self.session = requests.Session()

        self.min_command_interval = (
            min_command_interval if min_command_interval is not None
            else cfg.min_command_interval
        )
        # _last_sent[key] = (timestamp, last_sign)
        # last_sign — знак последней отправленной скорости (+1, -1, 0, или None).
        # Используется для bypass-а throttle при смене направления.
        self._last_sent = {}
        self.events = events
        self.metrics = metrics
        self.trace = trace
        # B4: health tracking — последний результат HTTP запроса к PTZ/imaging
        # _last_http = {"timestamp": monotonic, "ok": bool, "status": int|None, "error": str|None}
        self._last_http = None
        # Many cameras don't support ONVIF Imaging focus control (HTTP 400).
        # When disabled, focus() and stop_focus() are silent no-ops.
        self._enable_focus = cfg.enable_focus_control

    def _trace_http(self, service, **fields):
        if self.trace:
            self.trace.record("ptz_http", service=service, **fields)

    def _build_auth_header(self):
        nonce_bytes = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce_bytes).decode('utf-8')
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        sha = hashlib.sha1()
        sha.update(nonce_bytes)
        sha.update(created.encode('utf-8'))
        sha.update(self.password.encode('utf-8'))
        digest_b64 = base64.b64encode(sha.digest()).decode('utf-8')

        return f"""<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
          <wsse:UsernameToken>
            <wsse:Username>{self.username}</wsse:Username>
            <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd#PasswordDigest">{digest_b64}</wsse:Password>
            <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd#Base64Binary">{nonce_b64}</wsse:Nonce>
            <wsu:Created>{created}</wsu:Created>
          </wsse:UsernameToken>
        </wsse:Security>"""

    def _throttled(self, key, value=None):
        """Returns True if command should be dropped by throttle.

        B2: bypasses throttle when the new value's sign differs from the
        last sent value's sign (direction change). This eliminates the
        ~150ms lag when target crosses frame center and pan must reverse.

        Args:
            key: throttle bucket key ("move", "zoom", "focus")
            value: new speed value (signed). If sign differs from last → bypass.
                   If None (e.g. for stop commands), normal throttle applies.
        """
        now = time.monotonic()
        last_entry = self._last_sent.get(key)
        last_time = last_entry[0] if last_entry else 0
        last_sign = last_entry[1] if last_entry else None

        new_sign = self._sign(value) if value is not None else None

        # B2: direction change bypass — если знак поменялся, не троттлим
        if (new_sign is not None and last_sign is not None
                and new_sign != 0 and last_sign != 0
                and new_sign != last_sign):
            self._last_sent[key] = (now, new_sign)
            return False  # не троттлить — пропустить команду

        if now - last_time < self.min_command_interval:
            return True
        self._last_sent[key] = (now, new_sign)
        return False

    @staticmethod
    def _sign(value):
        if value > 0:
            return 1
        if value < 0:
            return -1
        return 0

    def _post_ptz(self, body_xml, key=None, force=False, value=None, suppress_error=False):
        throttled = bool(key) and not force and self._throttled(key, value)
        if throttled:
            if self.events:
                self.events.emit("command_throttled", f"ptz:{key}")
            self._trace_http("ptz", key=key, throttled=True, sent=False)
            return False
        try:
            resp = self.session.post(self.ptz_url, data=body_xml, headers=self.headers, timeout=self._http_timeout)
            ok = resp.status_code == 200
            self._last_http = {
                "timestamp": time.monotonic(),
                "ok": ok,
                "status": resp.status_code,
                "error": None,
            }
            if not ok:
                if suppress_error:
                    logger.debug("PTZ %s -> HTTP %s (suppressed)", key or 'stop', resp.status_code)
                else:
                    logger.warning("PTZ %s -> HTTP %s", key or 'stop', resp.status_code)
                    self._record_error(f"ptz_http_{resp.status_code}:{key}")
            self._trace_http("ptz", key=key, throttled=False, sent=True,
                             http=resp.status_code, ok=ok)
            return True
        except requests.exceptions.RequestException as e:
            logger.error("PTZ Error (%s): %s", key or 'stop', e)
            if not suppress_error:
                self._record_error(f"ptz:{key or 'stop'}:{e}")
            self._last_http = {
                "timestamp": time.monotonic(),
                "ok": False,
                "status": None,
                "error": type(e).__name__,
            }
            self._trace_http("ptz", key=key, throttled=False, sent=False,
                             error=type(e).__name__)
            return False

    def _post_imaging(self, body_xml, key=None, force=False, value=None, suppress_error=False):
        throttled = bool(key) and not force and self._throttled(key, value)
        if throttled:
            if self.events:
                self.events.emit("command_throttled", f"imaging:{key}")
            self._trace_http("imaging", key=key, throttled=True, sent=False)
            return False
        try:
            resp = self.session.post(self.imaging_url, data=body_xml, headers=self.headers, timeout=self._http_timeout)
            ok = resp.status_code == 200
            self._last_http = {
                "timestamp": time.monotonic(),
                "ok": ok,
                "status": resp.status_code,
                "error": None,
            }
            if not ok:
                if suppress_error:
                    # Best-effort operation (e.g. stop_focus on cameras that
                    # don't support it) — log at debug level, don't bump error counter
                    logger.debug("Imaging %s -> HTTP %s (suppressed)", key or 'stop', resp.status_code)
                else:
                    logger.warning("Imaging %s -> HTTP %s", key or 'stop', resp.status_code)
                    self._record_error(f"imaging_http_{resp.status_code}:{key}")
            self._trace_http("imaging", key=key, throttled=False, sent=True,
                             http=resp.status_code, ok=ok)
            return True
        except requests.exceptions.RequestException as e:
            logger.error("Imaging Error (%s): %s", key or 'stop', e)
            if not suppress_error:
                self._record_error(f"imaging:{key or 'stop'}:{e}")
            self._last_http = {
                "timestamp": time.monotonic(),
                "ok": False,
                "status": None,
                "error": type(e).__name__,
            }
            self._trace_http("imaging", key=key, throttled=False, sent=False,
                             error=type(e).__name__)
            return False

    def _record_command(self, detail):
        if self.metrics:
            self.metrics.ptz_command()
        if self.events:
            self.events.emit("move_started", detail)

    def _record_finished(self, detail):
        if self.events:
            self.events.emit("move_finished", detail)

    def _record_error(self, detail):
        if self.metrics:
            self.metrics.error()
        if self.events:
            self.events.emit("error", detail)

    def _safe_speed(self, value, axis):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            self._record_error(f"invalid_{axis}_speed")
            return None
        return max(-1.0, min(1.0, float(value)))

    def health(self):
        """B4: возвращает health-словарь для /api/status.

        Поля:
          ptz_reachable — True если последний HTTP к PTZ/imaging был успешным (200)
                          и был в течение последних 30 секунд
          last_http_ok — bool: последний запрос успешен?
          last_http_age_s — сколько секунд назад был последний HTTP запрос (None если не было)
          last_http_status — HTTP status code последнего запроса (None если не было или network error)
          last_http_error — имя класса исключения если был network error (None иначе)
          ptz_url — URL PTZ endpoint (для отладки)
        """
        now = time.monotonic()
        if self._last_http is None:
            return {
                "ptz_reachable": None,  # неизвестно — ещё не было запросов
                "last_http_ok": None,
                "last_http_age_s": None,
                "last_http_status": None,
                "last_http_error": None,
                "ptz_url": self.ptz_url,
            }
        age = round(now - self._last_http["timestamp"], 2)
        # Считаем reachable если последний запрос был OK и не старше 30 сек
        ptz_reachable = (
            self._last_http["ok"]
            and age < 30.0
        )
        return {
            "ptz_reachable": ptz_reachable,
            "last_http_ok": self._last_http["ok"],
            "last_http_age_s": age,
            "last_http_status": self._last_http["status"],
            "last_http_error": self._last_http["error"],
            "ptz_url": self.ptz_url,
        }

    def move(self, pan, tilt):
        pan = self._safe_speed(pan, "pan")
        tilt = self._safe_speed(tilt, "tilt")
        if pan is None or tilt is None:
            return
        if self.trace:
            self.trace.record("ptz_command", kind="move", pan=pan, tilt=tilt)
        move_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Header>{self._build_auth_header()}</s:Header>
          <s:Body>
            <tptz:ContinuousMove>
              <tptz:ProfileToken>{self.profile}</tptz:ProfileToken>
              <tptz:Velocity>
                <tt:PanTilt x="{pan}" y="{tilt}"/>
                <tt:Zoom x="0"/>
              </tptz:Velocity>
            </tptz:ContinuousMove>
          </s:Body>
        </s:Envelope>"""
        if self._post_ptz(move_xml, key="move", value=pan or tilt):
            self._record_command(f"pan={pan},tilt={tilt}")

    def zoom(self, speed):
        speed = self._safe_speed(speed, "zoom")
        if speed is None:
            return
        if self.trace:
            self.trace.record("ptz_command", kind="zoom", speed=speed)
        zoom_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Header>{self._build_auth_header()}</s:Header>
          <s:Body>
            <tptz:ContinuousMove>
              <tptz:ProfileToken>{self.profile}</tptz:ProfileToken>
              <tptz:Velocity>
                <tt:PanTilt x="0" y="0"/>
                <tt:Zoom x="{speed}"/>
              </tptz:Velocity>
            </tptz:ContinuousMove>
          </s:Body>
        </s:Envelope>"""
        if self._post_ptz(zoom_xml, key="zoom", value=speed):
            self._record_command(f"zoom={speed}")

    def focus(self, speed):
        # Skip focus commands entirely if camera doesn't support Imaging service
        if not self._enable_focus:
            return
        speed = self._safe_speed(speed, "focus")
        if speed is None:
            return
        if self.trace:
            self.trace.record("ptz_command", kind="focus", speed=speed)
        focus_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Header>{self._build_auth_header()}</s:Header>
          <s:Body>
            <timg:Move>
              <timg:VideoSourceToken>{self.video_source}</timg:VideoSourceToken>
              <timg:Focus>
                <tt:Continuous>
                  <tt:Speed>{speed}</tt:Speed>
                </tt:Continuous>
              </timg:Focus>
            </timg:Move>
          </s:Body>
        </s:Envelope>"""
        # suppress_error: even with enable_focus, some cameras reject Move on
        # certain profiles. Don't flood logs — focus is best-effort.
        if self._post_imaging(focus_xml, key="focus", value=speed, suppress_error=True):
            self._record_command(f"focus={speed}")

    def _send_ptz_stop(self, pantilt, zoom):
        stop_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
          <s:Header>{self._build_auth_header()}</s:Header>
          <s:Body>
            <tptz:Stop>
              <tptz:ProfileToken>{self.profile}</tptz:ProfileToken>
              <tptz:PanTilt>{"true" if pantilt else "false"}</tptz:PanTilt>
              <tptz:Zoom>{"true" if zoom else "false"}</tptz:Zoom>
            </tptz:Stop>
          </s:Body>
        </s:Envelope>"""
        return self._post_ptz(stop_xml, force=True)

    def stop_pantilt(self):
        # Сброс throttle bucket для move (после stop направление теряется)
        self._last_sent.pop("move", None)
        if self._send_ptz_stop(pantilt=True, zoom=False):
            self._record_finished("pantilt")

    def stop_zoom(self):
        self._last_sent.pop("zoom", None)
        if self._send_ptz_stop(pantilt=False, zoom=True):
            self._record_finished("zoom")

    def stop_focus(self):
        """Stop continuous focus.

        Many cameras don't support ONVIF `timg:Stop` for imaging service and
        return HTTP 400. We use `timg:Move` with Speed=0 instead, which is
        the standard ONVIF way to stop continuous focus and works on most
        cameras. If the camera still rejects it, the error is logged but
        doesn't break the pipeline (focus stops automatically when the next
        command arrives or when zoom stops).
        """
        # Skip entirely if focus control is disabled
        if not self._enable_focus:
            return
        self._last_sent.pop("focus", None)
        # Use Move with Speed=0 instead of Stop — more widely supported
        stop_f_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Header>{self._build_auth_header()}</s:Header>
          <s:Body>
            <timg:Move>
              <timg:VideoSourceToken>{self.video_source}</timg:VideoSourceToken>
              <timg:Focus>
                <tt:Continuous>
                  <tt:Speed>0</tt:Speed>
                </tt:Continuous>
              </timg:Focus>
            </timg:Move>
          </s:Body>
        </s:Envelope>"""
        # force=True + suppress errors: focus stop is best-effort, not critical
        ok = self._post_imaging(stop_f_xml, force=True, suppress_error=True)
        if ok:
            self._record_finished("focus")

    def stop(self):
        self._last_sent.clear()
        if self._send_ptz_stop(pantilt=True, zoom=True):
            self._record_finished("all")
        self.stop_focus()

    def goto_home(self, pan=0.0, tilt=0.0, zoom=0.0):
        """Move camera to absolute home position (pan=0, tilt=0, zoom=1x).

        Called when target is lost — returns camera to center before patrol
        starts, so patrol doesn't begin from a random position the camera
        ended up in while tracking.

        Uses ONVIF AbsoluteMove operation. Values are in normalized space:
          pan:  -1.0 (left) to 1.0 (right), 0.0 = center
          tilt: -1.0 (down) to 1.0 (up), 0.0 = center
          zoom:  0.0 (wide/1x) to 1.0 (telephoto/max zoom)

        If camera doesn't support AbsoluteMove (returns HTTP 400),
        error is suppressed — patrol will still work from current position.
        """
        # Clear throttle buckets — fresh start after goto_home
        self._last_sent.clear()
        home_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Header>{self._build_auth_header()}</s:Header>
          <s:Body>
            <tptz:AbsoluteMove>
              <tptz:ProfileToken>{self.profile}</tptz:ProfileToken>
              <tptz:Position>
                <tt:PanTilt x="{pan}" y="{tilt}"/>
                <tt:Zoom x="{zoom}"/>
              </tptz:Position>
            </tptz:AbsoluteMove>
          </s:Body>
        </s:Envelope>"""
        if self._post_ptz(home_xml, force=True, suppress_error=True):
            if self.events:
                self.events.emit("move_finished", f"goto_home pan={pan},tilt={tilt},zoom={zoom}")
            return True
        return False
