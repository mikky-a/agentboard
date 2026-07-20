// Рисует иконку приложения: тёмная доска с карточками-агентами.
import Cocoa

let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()

// фон-доска
let bg = NSBezierPath(roundedRect: NSRect(x: 60, y: 60, width: 904, height: 904),
                      xRadius: 190, yRadius: 190)
NSColor(red: 0.055, green: 0.09, blue: 0.075, alpha: 1).setFill()
bg.fill()

// карточка с цветной точкой-статусом
func card(_ x: CGFloat, _ y: CGFloat, _ dot: NSColor) {
    let r = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: 340, height: 250),
                         xRadius: 44, yRadius: 44)
    NSColor(red: 0.1, green: 0.145, blue: 0.12, alpha: 1).setFill()
    r.fill()
    dot.setFill()
    NSBezierPath(ovalIn: NSRect(x: x + 42, y: y + 158, width: 50, height: 50)).fill()
    NSColor(red: 0.35, green: 0.42, blue: 0.38, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: x + 42, y: y + 96, width: 256, height: 26),
                 xRadius: 13, yRadius: 13).fill()
    NSBezierPath(roundedRect: NSRect(x: x + 42, y: y + 48, width: 180, height: 26),
                 xRadius: 13, yRadius: 13).fill()
}

let green = NSColor(red: 0.36, green: 0.85, blue: 0.54, alpha: 1)
let amber = NSColor(red: 0.91, green: 0.71, blue: 0.29, alpha: 1)
let grey  = NSColor(red: 0.33, green: 0.38, blue: 0.36, alpha: 1)

card(152, 550, green)
card(532, 550, amber)
card(152, 226, grey)
card(532, 226, green)

img.unlockFocus()

let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: "icon_1024.png"))
print("icon_1024.png готова")
