"""Geography Explorer 3 — bilingual, offline, educational solar-time explorer.

Python 3.11+; PySide6>=6.8,<7.
Required assets beside this file: language.json and data/land.geojson.
Bundled Vazirmatn fonts are loaded automatically from fonts/.
A's entered Gregorian date/time remains fixed; without A, use live UTC.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QTimer, QDate, QSettings, QLocale
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPen, QKeySequence, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget, QProxyStyle, QStyle,
)

ROOT = Path(__file__).resolve().parent
VERSION = '3.0'
STEPS = (1, 5, 10)
PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')


class Translator:
    def __init__(self, path: Path, language: str = 'FA'):
        self.catalog = json.loads(path.read_text(encoding='utf-8-sig'))
        if set(self.catalog) != {'FA', 'English'}:
            raise ValueError('language.json must contain FA and English sections.')
        if set(self.catalog['FA']) != set(self.catalog['English']):
            raise ValueError('Both language sections must contain matching keys.')
        for section in self.catalog.values():
            if not isinstance(section, dict):
                raise ValueError('Invalid language section.')
            if not isinstance(section.get('weekdays'), list) or len(section['weekdays']) != 7:
                raise ValueError('Seven weekdays are required in each language.')
            if not all(isinstance(v, str) for k, v in section.items() if k != 'weekdays'):
                raise ValueError('Translation values must be strings.')
        self.language = language if language in self.catalog else 'FA'

    @property
    def rtl(self) -> bool:
        return self.language == 'FA'

    def text(self, key: str, **values) -> str:
        return self.catalog[self.language][key].format(**values)

    def number(self, value: object) -> str:
        text = str(value)
        return text.translate(PERSIAN_DIGITS) if self.rtl else text

    def weekday(self, value: datetime) -> str:
        return self.catalog[self.language]['weekdays'][value.weekday()]

    def date(self, value: datetime) -> str:
        return self.number(value.strftime('%Y/%m/%d'))

    def coordinate(self, value: int, latitude=False) -> str:
        if value == 0:
            return self.number(0) + '° — ' + self.text('equator' if latitude else 'greenwich')
        direction = ('north' if value > 0 else 'south') if latitude else ('east' if value > 0 else 'west')
        return self.number(abs(value)) + '° ' + self.text(direction)

    def offset(self, minutes: int) -> str:
        if minutes == 0:
            return self.text('no_offset')
        hours, mins = divmod(abs(minutes), 60)
        parts = []
        if hours:
            parts.append(self.text('hours', n=self.number(hours)))
        if mins:
            parts.append(self.text('minutes', n=self.number(mins)))
        return self.text('ahead' if minutes > 0 else 'behind', duration=self.text('join').join(parts))

    def relative_day(self, value: datetime, origin: datetime) -> str:
        count = (value.date() - origin.date()).days
        if count in (-1, 0, 1):
            return self.text({-1: 'yesterday', 0: 'same_day', 1: 'tomorrow'}[count])
        return self.text('days_after' if count > 0 else 'days_before', n=self.number(abs(count)))


def snap(value: float, limit: int, step: int = 1) -> int:
    rounded = math.copysign(math.floor(abs(value) / step + .5) * step, value)
    return int(max(-limit, min(limit, rounded)))


def utc_now() -> datetime:
    # Deliberately naive: these are educational wall times, not official zones.
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ReferencePoint:
    lon: int
    lat: int
    local: datetime

    def time_at(self, longitude: int) -> datetime:
        return self.local + timedelta(minutes=(longitude - self.lon) * 4)

    def as_dict(self) -> dict:
        return {'longitude': self.lon, 'latitude': self.lat,
                'local_datetime': self.local.isoformat(timespec='seconds')}


class SessionError(ValueError):
    """A localization key instead of an embedded language-specific error."""


def checked_coordinate(value: object, limit: int) -> int:
    if type(value) is not int or not -limit <= value <= limit:
        raise SessionError('invalid_coordinate')
    return value


def validate_session(data: object) -> dict:
    """Validate before changing application state. Accept version 2 sessions too."""
    if not isinstance(data, dict) or data.get('format') != 'geography-explorer' or type(data.get('version')) is not int or data['version'] not in (1, 2):
        raise SessionError('invalid_session')
    point, settings = data.get('point'), data.get('settings')
    if not isinstance(point, dict):
        raise SessionError('invalid_coordinate')
    lon = checked_coordinate(point.get('longitude'), 180)
    lat = checked_coordinate(point.get('latitude'), 90)
    if not isinstance(settings, dict):
        raise SessionError('invalid_settings')
    step = settings.get('step', 1)
    grid_geo = settings.get('grid_geo', settings.get('grid', True))
    grid_hours = settings.get('grid_hours', False)
    if type(step) is not int or step not in STEPS or type(grid_geo) is not bool or type(grid_hours) is not bool:
        raise SessionError('invalid_settings')
    reference = None
    item = data.get('reference')
    if item is not None:
        if not isinstance(item, dict):
            raise SessionError('invalid_reference')
        a_lon = checked_coordinate(item.get('longitude'), 180)
        a_lat = checked_coordinate(item.get('latitude'), 90)
        try:
            stamp = item['local_datetime']
            if not isinstance(stamp, str):
                raise ValueError()
            local = datetime.fromisoformat(stamp)
            if local.tzinfo is not None or not 1900 <= local.year <= 2100:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise SessionError('invalid_reference') from None
        reference = ReferencePoint(a_lon, a_lat, local.replace(microsecond=0))
    return {'lon': lon, 'lat': lat, 'reference': reference, 'step': step,
            'grid_geo': grid_geo, 'grid_hours': grid_hours}


class ClearCheckStyle(QProxyStyle):
    """A visible, drawn check mark in both themes; no external icon files."""
    def __init__(self):
        super().__init__('Fusion')

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            return super().drawPrimitive(element, option, painter, widget)
        dark = bool(QApplication.instance().property('darkTheme'))
        checked = bool(option.state & QStyle.StateFlag.State_On)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor('#3186e5' if checked else ('#9ab4cc' if dark else '#637f99')), 1.4))
        painter.setBrush(QColor('#267ce0' if checked else ('#172a3e' if dark else '#ffffff')))
        painter.drawRoundedRect(rect, 3, 3)
        if checked:
            pen = QPen(QColor('#ffffff'), 2.1)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(rect.left() + rect.width() * .2, rect.top() + rect.height() * .52)
            path.lineTo(rect.left() + rect.width() * .43, rect.top() + rect.height() * .74)
            path.lineTo(rect.left() + rect.width() * .82, rect.top() + rect.height() * .27)
            painter.drawPath(path)
        painter.restore()


class WorldMap(QWidget):
    selected = Signal(int, int)
    reference_requested = Signal(int, int)

    def __init__(self, translator: Translator, parent=None):
        super().__init__(parent)
        self.tr = translator
        self.setMinimumSize(700, 330)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.point = (0, 0)
        self.reference = None
        self.frozen = False
        self.step = 1
        self.grid_geo = True
        self.grid_hours = False
        self.dark = False
        self.area = QRectF()
        self.paths = []
        data = json.loads((ROOT / 'data' / 'land.geojson').read_text(encoding='utf-8'))
        for feature in data['features']:
            geometry = feature['geometry']
            if geometry['type'] == 'Polygon':
                polygons = [geometry['coordinates']]
            elif geometry['type'] == 'MultiPolygon':
                polygons = geometry['coordinates']
            else:
                continue
            for polygon in polygons:
                path = QPainterPath()
                path.setFillRule(Qt.FillRule.OddEvenFill)
                for ring in polygon:
                    if not ring:
                        continue
                    path.moveTo(ring[0][0], -ring[0][1])
                    for vertex in ring[1:]:
                        path.lineTo(vertex[0], -vertex[1])
                    path.closeSubpath()
                self.paths.append(path)
        if not self.paths:
            raise ValueError('No land polygons in data/land.geojson.')
        self._layout_map()

    def _layout_map(self):
        width = min(max(1, self.width() - 112), max(1, self.height() - 94) * 2.65)
        height = width / 2.65
        self.area = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def resizeEvent(self, event):
        minimum_height = max(330, math.ceil((self.width() - 112) / 2.65 + 94))
        if self.minimumHeight() != minimum_height:
            self.setMinimumHeight(minimum_height)
        self._layout_map()
        super().resizeEvent(event)

    def project(self, longitude: int, latitude: int) -> QPointF:
        a = self.area
        return QPointF(a.left() + (longitude + 180) / 360 * a.width(), a.top() + (90 - latitude) / 180 * a.height())

    def coordinate_at(self, position: QPointF):
        if not self.area.contains(position):
            return None
        a = self.area
        lon = snap((position.x() - a.left()) / a.width() * 360 - 180, 180, self.step)
        lat = snap(90 - (position.y() - a.top()) / a.height() * 180, 90, self.step)
        return lon, lat

    def mouseMoveEvent(self, event):
        point = self.coordinate_at(event.position())
        if point is not None and not self.frozen:
            self.point = point
            self.selected.emit(*point)
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        point = self.coordinate_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and point is not None:
            self.reference_requested.emit(*point)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#182a3d' if self.dark else '#ffffff'))
        p.fillRect(self.area, QColor('#223f55' if self.dark else '#eaf5fb'))
        p.save()
        p.setClipRect(self.area)
        p.save()
        p.translate(self.area.center())
        p.scale(self.area.width() / 360, self.area.height() / 180)
        outline = QPen(QColor('#7d9f87'), .65)
        outline.setCosmetic(True)
        p.setPen(outline)
        p.setBrush(QColor('#4d755e' if self.dark else '#c2dcb5'))
        for path in self.paths:
            p.drawPath(path)
        p.restore()
        if self.grid_geo:
            p.setPen(QPen(QColor('#62859f' if self.dark else '#92b6cd'), .65))
            for lon in range(-180, 181, 10):
                p.drawLine(self.project(lon, -90), self.project(lon, 90))
            for lat in range(-90, 91, 10):
                p.drawLine(self.project(-180, lat), self.project(180, lat))
        if self.grid_hours:
            p.setPen(QPen(QColor('#c4a3f5' if self.dark else '#8866bc'), 1.15, Qt.PenStyle.DashLine))
            for lon in range(-180, 181, 15):
                p.drawLine(self.project(lon, -90), self.project(lon, 90))
        # Equator and Greenwich remain as geographic reference axes.
        p.setPen(QPen(QColor('#e5ad42'), 1.6))
        p.drawLine(self.project(0, -90), self.project(0, 90))
        p.drawLine(self.project(-180, 0), self.project(180, 0))
        lon, lat = self.point
        position = self.project(lon, lat)
        p.setPen(QPen(QColor('#ed805b'), 1.3, Qt.PenStyle.DashLine))
        p.drawLine(self.project(lon, -90), self.project(lon, 90))
        p.drawLine(self.project(-180, lat), self.project(180, lat))
        p.setBrush(QColor('#ee7952'))
        p.setPen(QPen(QColor('white'), 2))
        p.drawEllipse(position, 5, 5)
        if self.reference is not None:
            p.setBrush(QColor('#2879f0'))
            p.drawEllipse(self.project(*self.reference), 7, 7)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor('#7798b0' if self.dark else '#b0c9d9'), 1))
        p.drawRect(self.area)
        p.setFont(QFont(self.font().family(), 8))
        if self.grid_geo:
            p.setPen(QColor('#c1d5e6' if self.dark else '#47647c'))
            for lon in range(-180, 181, 10):
                x = self.project(lon, 0).x()
                for y in (self.area.top() - 22, self.area.bottom() + 3):
                    p.drawText(QRectF(x - 15, y, 30, 19), Qt.AlignmentFlag.AlignCenter, self.tr.number(abs(lon)))
            for lat in range(-90, 91, 10):
                y = self.project(0, lat).y()
                for x in (self.area.left() - 45, self.area.right() + 5):
                    p.drawText(QRectF(x, y - 9, 40, 18), Qt.AlignmentFlag.AlignCenter, self.tr.number(abs(lat)) + '°')
        if self.grid_hours:
            p.setPen(QColor('#d1b2ff' if self.dark else '#7953af'))
            for lon in range(-180, 181, 15):
                x = self.project(lon, 0).x()
                sign = '+' if lon > 0 else ('−' if lon < 0 else '')
                mark = self.tr.text('hour_mark', sign=sign, n=self.tr.number(abs(lon) // 15))
                for y in (self.area.top() - 43, self.area.bottom() + 23):
                    p.drawText(QRectF(x - 21, y, 42, 18), Qt.AlignmentFlag.AlignCenter, mark)
        if self.reference is not None:
            position = self.project(*self.reference)
            label = QRectF(min(position.x() + 10, self.area.right() - 29), max(self.area.top() + 2, position.y() - 28), 26, 23)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor('#2879f0'))
            p.drawRoundedRect(label, 6, 6)
            p.setFont(QFont(self.font().family(), 10, QFont.Weight.Bold))
            p.setPen(QColor('white'))
            p.drawText(label, Qt.AlignmentFlag.AlignCenter, 'A')


class DropdownArrow:
    """Explicit arrows remain visible with a styled light or dark palette."""
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = 14 if self.layoutDirection() == Qt.LayoutDirection.RightToLeft else self.width() - 14
        y = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#c8dced' if QApplication.instance().property('darkTheme') else '#516f89'))
        painter.drawPolygon(QPolygonF([QPointF(x - 4, y - 2), QPointF(x + 4, y - 2), QPointF(x, y + 3)]))


class SelectBox(DropdownArrow, QComboBox):
    pass


class DateSelector(DropdownArrow, QDateEdit):
    pass


class ReferenceDialog(QDialog):
    """Compact, fixed-size modal with hour/minute/second drop-downs."""
    def __init__(self, initial: datetime, tr: Translator, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.text('reference_title'))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if tr.rtl else Qt.LayoutDirection.LeftToRight)
        self.setFixedSize(460, 218)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        form = QFormLayout()
        form.setVerticalSpacing(13)
        self.date = DateSelector(QDate(initial.year, initial.month, initial.day))
        self.date.setDateRange(QDate(1900, 1, 1), QDate(2100, 12, 31))
        self.date.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.date.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        self.date.setDisplayFormat('yyyy/MM/dd')
        self.date.setCalendarPopup(True)
        form.addRow(tr.text('date_label'), self.date)
        clock = QWidget()
        clock.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        row = QHBoxLayout(clock)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.hour, self.minute, self.second = SelectBox(), SelectBox(), SelectBox()
        for combo, count, selected, label_key in ((self.hour, 24, initial.hour, 'hour'), (self.minute, 60, initial.minute, 'minute'), (self.second, 60, initial.second, 'second')):
            field = QVBoxLayout()
            field.setSpacing(4)
            label = QLabel(tr.text(label_key))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            field.addWidget(label)
            combo.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            for value in range(count):
                combo.addItem(tr.number(f'{value:02d}'), value)
            combo.setCurrentIndex(selected)
            combo.setMaxVisibleItems(10)
            combo.setMinimumWidth(77)
            field.addWidget(combo)
            row.addLayout(field)
        form.addRow(tr.text('time_label'), clock)
        layout.addLayout(form)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr.text('apply'))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr.text('cancel'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> datetime:
        date = self.date.date()
        return datetime(date.year(), date.month(), date.day(), self.hour.currentData(), self.minute.currentData(), self.second.currentData())


class SettingsDialog(QDialog):
    def __init__(self, tr: Translator, step: int, font_size: int, dark: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.text('settings_title'))
        self.setMinimumWidth(420)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if tr.rtl else Qt.LayoutDirection.LeftToRight)
        form = QFormLayout(self)
        form.setContentsMargins(20, 18, 20, 18)
        form.setVerticalSpacing(12)
        self.language = SelectBox()
        self.language.addItem('فارسی', 'FA')
        self.language.addItem('English', 'English')
        self.language.setCurrentIndex(0 if tr.rtl else 1)
        self.step = SelectBox()
        for value in STEPS:
            self.step.addItem(tr.text('degrees', n=tr.number(value)), value)
        self.step.setCurrentIndex(STEPS.index(step))
        self.font_size = SelectBox()
        for value in range(10, 15):
            self.font_size.addItem(tr.number(value), value)
        self.font_size.setCurrentIndex(font_size - 10)
        self.dark = QCheckBox(tr.text('dark_theme'))
        self.dark.setChecked(dark)
        form.addRow(tr.text('language'), self.language)
        form.addRow(tr.text('precision'), self.step)
        form.addRow(tr.text('font_size'), self.font_size)
        form.addRow(self.dark)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr.text('save'))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr.text('cancel'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class InfoCard(QFrame):
    def __init__(self, clock=False):
        super().__init__()
        self.setObjectName('card')
        self.setMinimumWidth(217)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)
        self.title = QLabel()
        self.title.setObjectName('muted')
        self.value = QLabel()
        self.value.setObjectName('clockValue' if clock else 'cardValue')
        self.value.setWordWrap(True)
        self.detail = QLabel()
        self.detail.setObjectName('muted')
        self.detail.setWordWrap(True)
        for label in (self.title, self.value, self.detail):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        self.detail.setVisible(clock)


class Explorer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings('GeographyExplorer', 'DesktopV3')
        self.tr = Translator(ROOT / 'data' / 'language.json', str(self.settings.value('language', 'FA')))
        self.setMinimumSize(1100, 720)
        self.resize(1280, 900)
        self.lon = self.lat = 0
        self.reference = None
        self.step = self._saved_int('step', 1, STEPS)
        self.font_size = self._saved_int('font_size', 10, tuple(range(10, 15)))
        self.dark = self._saved_bool('dark', False)
        self.grid_geo = self._saved_bool('grid_geo', True)
        self.grid_hours = self._saved_bool('grid_hours', False)
        self._build_ui()
        self.apply_language()
        self.apply_display()
        self.map.selected.connect(self.select_point)
        self.map.reference_requested.connect(self.choose_reference)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()
        self.statusBar().showMessage(self.tr.text('ready'))

    def _saved_int(self, key, default, allowed):
        try:
            result = int(self.settings.value(key, default))
            return result if result in allowed else default
        except (TypeError, ValueError):
            return default

    def _saved_bool(self, key, default):
        return str(self.settings.value(key, default)).lower() == 'true'

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(scroll)
        self.content = QWidget()
        scroll.setWidget(self.content)
        root = QVBoxLayout(self.content)
        root.setContentsMargins(22, 14, 22, 14)
        root.setSpacing(12)
        header = QFrame()
        header.setObjectName('header')
        hl = QVBoxLayout(header)
        hl.setContentsMargins(18, 13, 18, 13)
        hl.setSpacing(4)
        self.title = QLabel()
        self.title.setObjectName('title')
        self.byline = QLabel()
        self.byline.setObjectName('byline')
        for label in (self.title, self.byline):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hl.addWidget(label)
        root.addWidget(header)
        map_frame = QFrame()
        map_frame.setObjectName('mapFrame')
        ml = QVBoxLayout(map_frame)
        ml.setContentsMargins(8, 8, 8, 5)
        toggles = QHBoxLayout()
        self.geo_check = QCheckBox()
        self.hour_check = QCheckBox()
        self.geo_check.setChecked(self.grid_geo)
        self.hour_check.setChecked(self.grid_hours)
        self.geo_check.toggled.connect(self.toggle_geo_grid)
        self.hour_check.toggled.connect(self.toggle_hour_grid)
        toggles.addWidget(self.geo_check)
        toggles.addSpacing(14)
        toggles.addWidget(self.hour_check)
        toggles.addStretch()
        ml.addLayout(toggles)
        self.map = WorldMap(self.tr)
        ml.addWidget(self.map, 1)
        root.addWidget(map_frame, 1)
        ref_frame = QFrame()
        ref_frame.setObjectName('referencePanel')
        rl = QHBoxLayout(ref_frame)
        rl.setContentsMargins(14, 9, 14, 9)
        self.ref_label = QLabel()
        self.ref_label.setWordWrap(True)
        rl.addWidget(self.ref_label, 1)
        self.freeze_button = QPushButton()
        self.freeze_button.setCheckable(True)
        self.freeze_button.toggled.connect(self.set_frozen)
        self.edit_button = QPushButton()
        self.edit_button.clicked.connect(self.edit_reference)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self.clear_reference)
        for button in (self.freeze_button, self.edit_button, self.clear_button):
            rl.addWidget(button)
        root.addWidget(ref_frame)
        cards = QHBoxLayout()
        self.cards = {}
        for key in ('lat', 'lon', 'offset', 'clock'):
            card = InfoCard(key == 'clock')
            self.cards[key] = card
            cards.addWidget(card, 1)
        root.addLayout(cards)
        self.utc_label = QLabel()
        self.utc_label.setObjectName('utcLine')
        self.utc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.utc_label.setWordWrap(True)
        root.addWidget(self.utc_label)
        # No View menu: keep shortcuts as window actions.
        fullscreen = QAction(self)
        fullscreen.setShortcut(QKeySequence('F11'))
        fullscreen.triggered.connect(self.toggle_fullscreen)
        self.addAction(fullscreen)
        leave = QAction(self)
        leave.setShortcut(QKeySequence('Esc'))
        leave.triggered.connect(self.leave_fullscreen)
        self.addAction(leave)

    def _menu_action(self, menu, key, callback, shortcut=None):
        action = QAction(self.tr.text(key), menu)
        action.triggered.connect(callback)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        menu.addAction(action)
        return action

    def _build_menus(self):
        # Explicit LTR menubar keeps File / Tools / Help on the left in both languages.
        bar = self.menuBar()
        bar.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        previous = getattr(self, '_menus', [])
        bar.clear()
        for menu in previous:
            menu.deleteLater()
        previous_group = getattr(self, '_language_group', None)
        if previous_group is not None:
            previous_group.deleteLater()
        files, tools, help_menu = (bar.addMenu(self.tr.text(key)) for key in ('file', 'tools', 'help'))
        self._menus = [files, tools, help_menu]
        for menu in self._menus:
            menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft if self.tr.rtl else Qt.LayoutDirection.LeftToRight)
        self._menu_action(files, 'new_session', self.new_session, 'Ctrl+N')
        self._menu_action(files, 'open_session', self.open_session, 'Ctrl+O')
        self._menu_action(files, 'save_session', self.save_session, 'Ctrl+S')
        files.addSeparator()
        self._menu_action(files, 'export_png', self.export_image, 'Ctrl+Shift+S')
        self._menu_action(files, 'export_csv', self.export_csv)
        files.addSeparator()
        self._menu_action(files, 'exit', self.close, 'Ctrl+Q')
        self._menu_action(tools, 'copy_point', self.copy_point, 'Ctrl+Shift+C')
        self._menu_action(tools, 'settings', self.show_settings, 'Ctrl+,')
        language_menu = tools.addMenu(self.tr.text('language'))
        self._language_group = QActionGroup(self)
        self._language_group.setExclusive(True)
        for language, key in (('FA', 'language_fa'), ('English', 'language_en')):
            action = QAction(self.tr.text(key), language_menu)
            action.setCheckable(True)
            action.setChecked(self.tr.language == language)
            action.triggered.connect(lambda checked=False, chosen=language: self.change_language(chosen))
            self._language_group.addAction(action)
            language_menu.addAction(action)
        self._menu_action(help_menu, 'about', self.show_about)

    def apply_language(self):
        direction = Qt.LayoutDirection.RightToLeft if self.tr.rtl else Qt.LayoutDirection.LeftToRight
        self.setLayoutDirection(direction)
        self.content.setLayoutDirection(direction)
        self.setWindowTitle(self.tr.text('title') + ' | Geography Explorer')
        self.title.setText(self.tr.text('title'))
        self.byline.setText(self.tr.text('byline', name=self.tr.text('author')))
        self.geo_check.setText(self.tr.text('geo_grid'))
        self.geo_check.setToolTip(self.tr.text('geo_tip'))
        self.hour_check.setText(self.tr.text('hour_grid'))
        self.hour_check.setToolTip(self.tr.text('hour_tip'))
        self.freeze_button.setText(self.tr.text('unfreeze' if self.map.frozen else 'freeze'))
        self.edit_button.setText(self.tr.text('edit_a'))
        self.clear_button.setText(self.tr.text('delete_a'))
        self.map.setToolTip(self.tr.text('map_tip'))
        self._build_menus()
        self.refresh()
        self.map.update()

    def change_language(self, language: str):
        if language not in ('FA', 'English'):
            return
        self.tr.language = language
        self.settings.setValue('language', language)
        self.apply_language()
        self.statusBar().showMessage(self.tr.text('ready'))

    def apply_display(self):
        app = QApplication.instance()
        app.setFont(QFont(app.font().family(), self.font_size))
        app.setProperty('darkTheme', self.dark)
        self.map.step = self.step
        self.map.grid_geo = self.grid_geo
        self.map.grid_hours = self.grid_hours
        self.map.dark = self.dark
        colors = {'bg': '#0f1d2b' if self.dark else '#eff4f9',
                  'panel': '#182a3d' if self.dark else '#ffffff',
                  'text': '#e5eef7' if self.dark else '#243e56',
                  'muted': '#aac1d5' if self.dark else '#5e758b',
                  'border': '#344d64' if self.dark else '#d4e1ed',
                  'blue': '#1b3654' if self.dark else '#edf5ff'}
        # Font sizes are relative to the configured point size.
        self.setStyleSheet('''
            QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: %(bg)s; }
            QWidget { color: %(text)s; }
            QLabel { background: transparent; }
            QFrame#header { background: #173751; border-radius: 15px; }
            QFrame#header QLabel { color: white; }
            QLabel#title { font-size: %(title_size)spt; font-weight: 700; }
            QLabel#byline { color: #d5e6f3; }
            QFrame#mapFrame, QFrame#card { background: %(panel)s; border: 1px solid %(border)s; border-radius: 12px; }
            QFrame#referencePanel { background: %(blue)s; border: 1px solid %(border)s; border-radius: 10px; }
            QLabel#muted, QLabel#utcLine { color: %(muted)s; }
            QLabel#cardValue { font-size: %(value_size)spt; font-weight: 700; }
            QLabel#clockValue { font-size: %(clock_size)spt; font-weight: 700; color: #2e86e6; }
            QPushButton { background: %(panel)s; border: 1px solid %(border)s; border-radius: 7px; padding: 7px 13px; }
            QPushButton:hover { border-color: #2e86e6; }
            QPushButton:checked { background: #2676c4; color: white; }
            QPushButton:disabled { color: %(muted)s; }
            QComboBox, QDateEdit, QSpinBox { background: %(panel)s; border: 1px solid %(border)s; border-radius: 6px; padding: 5px 7px; min-height: 24px; }
            QComboBox::drop-down, QDateEdit::drop-down { width: 22px; border: none; }
            QComboBox::down-arrow, QDateEdit::down-arrow { image: none; }
            QComboBox QAbstractItemView { background: %(panel)s; color: %(text)s; selection-background-color: #2676c4; selection-color: white; }
            QCheckBox { spacing: 7px; padding: 4px 3px; }
            QDialog { background: %(bg)s; }
            QMenuBar, QMenu, QStatusBar { background: %(panel)s; color: %(text)s; }
            QMenu::item { padding: 6px 18px; }
            QMenu::item:selected, QMenuBar::item:selected { background: %(blue)s; }
            QScrollBar:vertical { width: 12px; }
        ''' % {**colors, 'title_size': self.font_size + 8, 'value_size': self.font_size + 3, 'clock_size': self.font_size + 9})
        self.map.update()

    def toggle_geo_grid(self, checked):
        self.grid_geo = checked
        self.map.grid_geo = checked
        self.settings.setValue('grid_geo', checked)
        self.map.update()

    def toggle_hour_grid(self, checked):
        self.grid_hours = checked
        self.map.grid_hours = checked
        self.settings.setValue('grid_hours', checked)
        self.map.update()

    def select_point(self, lon, lat):
        self.lon, self.lat = lon, lat
        self.map.point = (lon, lat)
        self.map.update()
        self.refresh()

    def current_calculation(self, now=None):
        real_utc = utc_now() if now is None else now
        if self.reference is not None:
            local = self.reference.time_at(self.lon)
            origin = self.reference.local
            calculated_utc = self.reference.time_at(0)
            offset = (self.lon - self.reference.lon) * 4
            source = 'A'
        else:
            origin = calculated_utc = real_utc
            local = real_utc + timedelta(minutes=self.lon * 4)
            offset = self.lon * 4
            source = 'UTC'
        return {'local': local, 'origin': origin, 'utc': calculated_utc,
                'offset': offset, 'source': source, 'real_utc': real_utc}

    def refresh(self):
        tr = self.tr
        result = self.current_calculation()
        local, origin = result['local'], result['origin']
        self.cards['lat'].title.setText(tr.text('latitude'))
        self.cards['lat'].value.setText(tr.coordinate(self.lat, True))
        self.cards['lon'].title.setText(tr.text('longitude'))
        self.cards['lon'].value.setText(tr.coordinate(self.lon))
        self.cards['offset'].title.setText(tr.text('offset_a' if self.reference else 'offset_utc'))
        self.cards['offset'].value.setText(tr.offset(result['offset']))
        self.cards['clock'].title.setText(tr.text('local_time'))
        self.cards['clock'].value.setText(tr.number(local.strftime('%H:%M:%S')))
        relative = tr.text('relative', relation=tr.relative_day(local, origin), origin='A' if self.reference else tr.text('greenwich'))
        self.cards['clock'].detail.setText(tr.text('date_detail', weekday=tr.weekday(local), day=tr.number(local.day), date=tr.date(local), relative=relative))
        stamp = result['utc']
        self.utc_label.setText(tr.text('greenwich_line', basis=tr.text('basis_a' if self.reference else 'basis_live'), clock=tr.number(stamp.strftime('%H:%M:%S')), weekday=tr.weekday(stamp), date=tr.date(stamp)))
        if self.reference is not None:
            a = self.reference
            self.ref_label.setText(tr.text('a_summary', lon=tr.coordinate(a.lon), lat=tr.coordinate(a.lat, True), clock=tr.number(a.local.strftime('%H:%M:%S')), weekday=tr.weekday(a.local), date=tr.date(a.local)))
        else:
            self.ref_label.setText(tr.text('no_a'))
        self.edit_button.setEnabled(self.reference is not None)
        self.clear_button.setEnabled(self.reference is not None)

    def choose_reference(self, lon, lat):
        initial = self.reference.time_at(lon) if self.reference else utc_now() + timedelta(minutes=lon * 4)
        dialog = ReferenceDialog(initial, self.tr, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.reference = ReferencePoint(lon, lat, dialog.value())
        self.map.reference = (lon, lat)
        self.freeze_button.setChecked(False)
        self.select_point(lon, lat)
        self.statusBar().showMessage(self.tr.text('a_saved'), 4000)

    def edit_reference(self):
        if self.reference is None:
            QMessageBox.information(self, self.tr.text('reference_title'), self.tr.text('select_a'))
            return
        # Keep the inspected point unchanged when editing A's time.
        point, frozen = (self.lon, self.lat), self.map.frozen
        initial = self.reference.local
        dialog = ReferenceDialog(initial, self.tr, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.reference = ReferencePoint(self.reference.lon, self.reference.lat, dialog.value())
        self.select_point(*point)
        self.freeze_button.setChecked(frozen)
        self.statusBar().showMessage(self.tr.text('a_saved'), 4000)

    def clear_reference(self):
        self.reference = None
        self.map.reference = None
        self.map.update()
        self.refresh()
        self.statusBar().showMessage(self.tr.text('a_deleted'), 4000)

    def new_session(self):
        self.clear_reference()
        self.freeze_button.setChecked(False)
        self.select_point(0, 0)
        self.statusBar().showMessage(self.tr.text('ready'), 4000)

    def set_frozen(self, checked):
        self.map.frozen = checked
        self.freeze_button.setText(self.tr.text('unfreeze' if checked else 'freeze'))
        self.statusBar().showMessage(self.tr.text('frozen' if checked else 'moving'), 4000)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()

    def leave_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized() if getattr(self, '_was_maximized', False) else self.showNormal()

    def show_settings(self):
        dialog = SettingsDialog(self.tr, self.step, self.font_size, self.dark, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.step = dialog.step.currentData()
        self.font_size = dialog.font_size.currentData()
        self.dark = dialog.dark.isChecked()
        self.tr.language = dialog.language.currentData()
        for key, value in (('step', self.step), ('font_size', self.font_size), ('dark', self.dark), ('language', self.tr.language)):
            self.settings.setValue(key, value)
        self.apply_display()
        self.apply_language()
        self.select_point(snap(self.lon, 180, self.step), snap(self.lat, 90, self.step))

    def session_data(self):
        return {'format': 'geography-explorer', 'version': 2,
                'point': {'longitude': self.lon, 'latitude': self.lat},
                'reference': None if self.reference is None else self.reference.as_dict(),
                'settings': {'step': self.step, 'grid_geo': self.grid_geo, 'grid_hours': self.grid_hours}}

    def apply_session(self, data):
        session = validate_session(data)
        self.reference = session['reference']
        self.map.reference = None if self.reference is None else (self.reference.lon, self.reference.lat)
        self.step = session['step']
        # Set checkboxes explicitly; setters also update map flags and preferences.
        self.geo_check.setChecked(session['grid_geo'])
        self.hour_check.setChecked(session['grid_hours'])
        self.toggle_geo_grid(session['grid_geo'])
        self.toggle_hour_grid(session['grid_hours'])
        self.freeze_button.setChecked(True)
        self.apply_display()
        self.select_point(session['lon'], session['lat'])

    @staticmethod
    def with_suffix(path, suffix):
        file = Path(path)
        return file if file.suffix.lower() == suffix else file.with_suffix(suffix)

    def save_session(self):
        path, _ = QFileDialog.getSaveFileName(self, self.tr.text('save_session'), 'geography-session.json', self.tr.text('json_filter'))
        if not path:
            return
        try:
            self.with_suffix(path, '.json').write_text(json.dumps(self.session_data(), ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            self.file_error(exc)
            return
        self.statusBar().showMessage(self.tr.text('session_saved'), 4000)

    def open_session(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr.text('open_session'), '', self.tr.text('json_filter'))
        if not path:
            return
        try:
            file = Path(path)
            if file.stat().st_size > 2_000_000:
                raise SessionError('large_file')
            self.apply_session(json.loads(file.read_text(encoding='utf-8-sig')))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.file_error(exc)
            return
        self.statusBar().showMessage(self.tr.text('session_opened'), 6000)

    def report(self):
        result = self.current_calculation()
        local, a = result['local'], self.reference
        return {'source': result['source'], 'longitude': self.lon, 'latitude': self.lat,
                'local_datetime': local.isoformat(timespec='seconds'), 'day_of_month': local.day,
                'iso_weekday': local.isoweekday(), 'weekday': self.tr.weekday(local),
                'day_relative_to_origin': self.tr.relative_day(local, result['origin']),
                'offset_from_origin_minutes': result['offset'], 'calculated_greenwich': result['utc'].isoformat(timespec='seconds'),
                'reference_longitude': '' if a is None else a.lon,
                'reference_latitude': '' if a is None else a.lat,
                'reference_datetime': '' if a is None else a.local.isoformat(timespec='seconds')}

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, self.tr.text('export_csv'), 'geography-report.csv', self.tr.text('csv_filter'))
        if not path:
            return
        try:
            with self.with_suffix(path, '.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                report = self.report()
                writer = csv.DictWriter(stream, fieldnames=list(report))
                writer.writeheader()
                writer.writerow(report)
        except OSError as exc:
            self.file_error(exc)
            return
        self.statusBar().showMessage(self.tr.text('csv_saved'), 4000)

    def export_image(self):
        path, _ = QFileDialog.getSaveFileName(self, self.tr.text('export_png'), 'geography.png', self.tr.text('png_filter'))
        if not path:
            return
        self.refresh()
        self.content.repaint()
        if not self.content.grab().save(str(self.with_suffix(path, '.png')), 'PNG'):
            self.file_error(SessionError('image_failed'))
            return
        self.statusBar().showMessage(self.tr.text('image_saved'), 4000)

    def copy_point(self):
        result, tr = self.current_calculation(), self.tr
        local = result['local']
        text = '\n'.join((tr.text('title'), tr.coordinate(self.lon), tr.coordinate(self.lat, True),
                          tr.text('report_time', clock=tr.number(local.strftime('%H:%M:%S'))),
                          tr.weekday(local) + ' | ' + tr.date(local),
                          tr.text('relative', relation=tr.relative_day(local, result['origin']), origin='A' if self.reference else tr.text('greenwich')),
                          tr.text('report_offset', offset=tr.offset(result['offset']))))
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(tr.text('copied'), 4000)

    def file_error(self, error):
        if isinstance(error, SessionError):
            message = self.tr.text(str(error))
        elif isinstance(error, (json.JSONDecodeError, UnicodeError, TypeError, KeyError)):
            message = self.tr.text('invalid_session')
        else:
            message = str(error)
        QMessageBox.warning(self, self.tr.text('file_error'), message)

    def show_about(self):
        QMessageBox.about(self, self.tr.text('about'), self.tr.text('about_text', title=self.tr.text('title'), version=self.tr.number(VERSION), name=self.tr.text('author')))


def load_font(app):
    folder = ROOT / 'fonts'
    if folder.exists():
        for file in sorted(folder.glob('*.ttf')):
            QFontDatabase.addApplicationFont(str(file))
    available = set(QFontDatabase.families())
    family = next((name for name in ('Vazirmatn', 'Vazir', 'Tahoma', 'DejaVu Sans') if name in available), 'Sans Serif')
    app.setFont(QFont(family, 10))


def main():
    app = QApplication(sys.argv)
    app.setStyle(ClearCheckStyle())
    app.setApplicationName('Geography Explorer')
    app.setOrganizationName('GeographyExplorer')
    load_font(app)
    try:
        window = Explorer()
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        QMessageBox.critical(None, 'Startup Error / خطای راه‌اندازی',
                             'Keep language.json and data/land.geojson next to main.py.\n'
                             'فایل زبان و پوشه نقشه را کنار کد نگه دارید.\n' + str(exc))
        return 1
    screen = app.primaryScreen()
    if screen:
        geometry = screen.availableGeometry()
        if geometry.width() >= 1100 and geometry.height() >= 720:
            window.resize(max(1100, min(1280, geometry.width() - 30)), max(720, min(900, geometry.height() - 50)))
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
