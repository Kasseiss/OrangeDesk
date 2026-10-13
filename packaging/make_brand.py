from pathlib import Path
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QLinearGradient, QFont
from PySide6.QtWidgets import QApplication
app = QApplication([])
out = Path('.build/brand')
out.mkdir(parents=True, exist_ok=True)
image = QImage(256, 256, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
p = QPainter(image)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
gradient = QLinearGradient(0, 0, 256, 256)
gradient.setColorAt(0, QColor('#ffbb68')); gradient.setColorAt(1, QColor('#ff652b'))
p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor('#111722'))
p.drawRoundedRect(0, 0, 256, 256, 64, 64)
path = QPainterPath(QPointF(147, 38))
for x,y in [(62,145),(123,145),(109,222),(202,112),(143,112),(147,38)]:
    path.lineTo(x,y)
p.fillPath(path, gradient); p.end()
image.save(str(out / 'icon.png'))
wizard = QImage(330, 640, QImage.Format.Format_RGB32)
wizard.fill(QColor('#0c1019'))
p = QPainter(wizard); p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.drawImage(70, 82, image.scaled(190,190,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
p.setPen(QColor('#f8fafc')); p.setFont(QFont('Microsoft YaHei UI',22,QFont.Weight.Bold)); p.drawText(32,325,'快橙伴航')
p.setPen(QColor('#ff9b50')); p.setFont(QFont('Segoe UI',10)); p.drawText(36,363,'ORANGE COMPANION')
p.setPen(QColor('#9aa9bf')); p.setFont(QFont('Microsoft YaHei UI',10)); p.drawText(36,425,'连接保活 · 国内直连'); p.drawText(36,455,'原生 Qt · UI / CLI')
p.end(); wizard.save(str(out/'wizard.bmp'))
