"""Persian geography explorer; vector land data, no raster images."""
import json
import math
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QMessageBox

ROOT = Path(__file__).resolve().parent
DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
def fa(value):
    return str(value).translate(DIGITS)
def snap(value, limit):
    """Nearest ten degrees; ties away from zero, bounded at poles/dateline."""
    return max(-limit, min(limit, int(math.copysign(math.floor(abs(value)/10 + .5)*10, value))))
def coordinate(value, latitude=False):
    if value == 0:
        return '۰° — استوا' if latitude else '۰° — گرینویچ'
    direction = ('شمالی' if value > 0 else 'جنوبی') if latitude else ('شرقی' if value > 0 else 'غربی')
    return f'{fa(abs(value))}° {direction}'
def offset_text(minutes):
    if minutes == 0:
        return 'بدون اختلاف با گرینویچ'
    h, m = divmod(abs(minutes), 60)
    return f'{fa(h)} ساعت و {fa(m)} دقیقه ' + ('جلوتر' if minutes > 0 else 'عقب‌تر')

class WorldMap(QWidget):
    selected = Signal(int, int)
    def __init__(self):
        super().__init__()
        self.setFixedSize(1120, 440)
        self.setMouseTracking(True)
        self.area = QRectF(56, 26, 1008, 378)
        self.point = None
        self.frozen = False
        data = json.loads((ROOT/'data'/'land.geojson').read_text(encoding='utf-8'))
        self.land = QPainterPath()
        self.land.setFillRule(Qt.FillRule.OddEvenFill)
        for feature in data['features']:
            geometry = feature['geometry']
            polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
            for polygon in polygons:
                for ring in polygon:
                    if not ring:
                        continue
                    self.land.moveTo(self.project(*ring[0][:2]))
                    for vertex in ring[1:]:
                        self.land.lineTo(self.project(*vertex[:2]))
                    self.land.closeSubpath()
    def project(self, lon, lat):
        a = self.area
        return QPointF(a.left()+(lon+180)/360*a.width(), a.top()+(90-lat)/180*a.height())
    def mouseMoveEvent(self, event):
        if self.frozen or not self.area.contains(event.position()):
            return
        a = self.area
        lon = snap((event.position().x()-a.left())/a.width()*360-180, 180)
        lat = snap(90-(event.position().y()-a.top())/a.height()*180, 90)
        self.point = (lon, lat)
        self.selected.emit(lon, lat)
        self.update()
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.area.contains(event.position()):
            if not self.frozen:
                self.mouseMoveEvent(event)
            self.frozen = not self.frozen
            self.update()
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#ffffff'))
        p.fillRect(self.area, QColor('#e6f3fa'))
        p.save()
        p.setClipRect(self.area)
        p.setPen(QPen(QColor('#82a88d'), .6))
        p.setBrush(QColor('#bcd7ab'))
        p.drawPath(self.land)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for lon in range(-180,181,10):
            p.setPen(QPen(QColor('#789eb4') if lon else QColor('#e8a240'), 1.8 if lon == 0 else .55))
            p.drawLine(self.project(lon,-90), self.project(lon,90))
        for lat in range(-90,91,10):
            p.setPen(QPen(QColor('#789eb4') if lat else QColor('#e8a240'), 1.8 if lat == 0 else .55))
            p.drawLine(self.project(-180,lat), self.project(180,lat))
        if self.point:
            lon,lat = self.point
            pos = self.project(lon,lat)
            p.setPen(QPen(QColor('#e16b42'), 1.5, Qt.PenStyle.DashLine))
            p.drawLine(self.project(lon,-90),self.project(lon,90))
            p.drawLine(self.project(-180,lat),self.project(180,lat))
            p.setPen(QPen(QColor('#ffffff'),2))
            p.setBrush(QColor('#e16b42'))
            p.drawEllipse(pos,5,5)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor('#9dbaca'),1))
        p.drawRect(self.area)
        p.setFont(QFont(self.font().family(),8))
        p.setPen(QColor('#426078'))
        for lon in range(-180,181,10):
            x = self.project(lon,0).x()
            p.drawText(QRectF(x-14,5,28,18), Qt.AlignmentFlag.AlignCenter, fa(abs(lon)))
            p.drawText(QRectF(x-14,405,28,18), Qt.AlignmentFlag.AlignCenter, fa(abs(lon)))
        for lat in range(-90,91,10):
            y = self.project(0,lat).y()
            for x in (9,1074):
                p.drawText(QRectF(x,y-9,37,18),Qt.AlignmentFlag.AlignCenter,fa(abs(lat))+'°')

class Explorer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('مختصات جغرافیایی و ساعت محلی')
        self.setFixedSize(1180,760)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.lon = self.lat = 0
        self.setStyleSheet('''
            QWidget { background: #f0f4f8; color: #253c55; }
            QFrame#header { background: #162d48; border-radius: 16px; }
            QFrame#header QLabel { background: transparent; color: white; }
            QFrame#card { background: white; border: 1px solid #dde5ed; border-radius: 12px; }
            QFrame#card QLabel { background: transparent; }
        ''')
        root = QVBoxLayout(self)
        root.setContentsMargins(24,18,24,18)
        root.setSpacing(12)
        header = QFrame()
        header.setObjectName('header')
        h = QVBoxLayout(header)
        title = QLabel('مختصات جغرافیایی و ساعت محلی')
        title.setFont(QFont(self.font().family(),23,QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name = QLabel('تهیه‌کننده: محمد صدرا عین القضاتی | سال: 1405')
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(title)
        h.addWidget(name)
        root.addWidget(header)
        self.map = WorldMap()
        root.addWidget(self.map,0,Qt.AlignmentFlag.AlignHCenter)
        row = QHBoxLayout()
        self.values = {}
        for key,title in [('lat','عرض جغرافیایی'),('lon','طول جغرافیایی'),('offset','اختلاف با گرینویچ'),('clock','ساعت محلی آموزشی')]:
            frame = QFrame()
            frame.setObjectName('card')
            layout = QVBoxLayout(frame)
            label = QLabel(title)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value = QLabel('—')
            value.setFont(QFont(self.font().family(),13,QFont.Weight.Bold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
            layout.addWidget(value)
            self.values[key] = value
            row.addWidget(frame)
        root.addLayout(row)
        self.utc = QLabel()
        self.utc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.utc)
        guide = QLabel('راهنما: موس را روی نقشه حرکت دهید؛ مختصات به نزدیک‌ترین ۱۰ درجه گرد می‌شود. کلیک: ثابت‌کردن / آزادکردن نقطه.\nشرق: جلوتر از گرینویچ • غرب: عقب‌تر • هر ۱۵ درجه = ۱ ساعت • هر ۱ درجه = ۴ دقیقه\nساعت محلی آموزشی از طول جغرافیایی محاسبه می‌شود و ممکن است با ساعت رسمی کشورها متفاوت باشد.')
        guide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        guide.setWordWrap(True)
        guide.setFont(QFont(self.font().family(),10))
        root.addWidget(guide)
        self.map.selected.connect(self.update_position)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_clock)
        self.timer.start(1000)
        self.update_position(0,0)
    def update_position(self,lon,lat):
        self.lon,self.lat = lon,lat
        self.map.point = (lon,lat)
        self.map.update()
        self.values['lat'].setText(coordinate(lat,True))
        self.values['lon'].setText(coordinate(lon))
        self.values['offset'].setText(offset_text(lon*4))
        self.update_clock()
    def update_clock(self):
        now = datetime.now(timezone.utc)
        local = now+timedelta(minutes=self.lon*4)
        self.values['clock'].setText(fa(local.strftime('%H:%M:%S')))
        day_delta = (local.date()-now.date()).days
        day_note = { -1:'روز قبل',0:'همان روز',1:'روز بعد'}[day_delta]
        self.utc.setText('گرینویچ (UTC): '+fa(now.strftime('%H:%M:%S'))+'  |  تاریخ میلادی نقطه: '+fa(local.strftime('%Y/%m/%d'))+' — '+day_note)

def main():
    app = QApplication(sys.argv)
    fonts = ROOT/'fonts'
    if fonts.exists():
        for file in fonts.glob('*.ttf'):
            QFontDatabase.addApplicationFont(str(file))
    available = set(QFontDatabase.families())
    family = next((name for name in ['Vazirmatn','Vazir','Tahoma','DejaVu Sans'] if name in available),'Sans Serif')
    app.setFont(QFont(family,11))
    try:
        window = Explorer()
    except (OSError,ValueError,KeyError) as exc:
        QMessageBox.critical(None,'خطای فایل نقشه','فایل data/land.geojson در دسترس یا معتبر نیست.\n'+str(exc))
        return 1
    window.show()
    return app.exec()

if __name__ == '__main__':
    sys.exit(main())
