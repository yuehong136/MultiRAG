package utility

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"

	"golang.org/x/image/font"
	"golang.org/x/image/font/basicfont"
	"golang.org/x/image/math/fixed"
)

// RenderCaptchaPNG emits raster bytes only; neither a JSON answer nor SVG text
// is embedded in the response. Randomness errors cannot produce a weak image.
func RenderCaptchaPNG(text string) ([]byte, error) {
	if len(text) != OTPLength {
		return nil, fmt.Errorf("invalid captcha length")
	}
	var noise [512]byte
	if _, err := rand.Read(noise[:]); err != nil {
		return nil, err
	}
	img := image.NewRGBA(image.Rect(0, 0, 300, 120))
	draw.Draw(img, img.Bounds(), &image.Uniform{color.RGBA{245, 245, 250, 255}}, image.Point{}, draw.Src)
	for i, ch := range text {
		glyph := image.NewRGBA(image.Rect(0, 0, 12, 18))
		drawer := font.Drawer{Dst: glyph, Src: image.NewUniform(color.RGBA{20 + noise[i]%60, 20 + noise[i+4]%60, 40 + noise[i+8]%60, 255}), Face: basicfont.Face7x13, Dot: fixed.P(2, 14)}
		drawer.DrawString(string(ch))
		dx, dy := 20+i*65+int(noise[i+12]%9), 18+int(noise[i+16]%13)
		for y := 0; y < 18; y++ {
			for x := 0; x < 12; x++ {
				pixel := glyph.RGBAAt(x, y)
				if pixel.A == 0 {
					continue
				}
				for sy := 0; sy < 4; sy++ {
					for sx := 0; sx < 4; sx++ {
						img.SetRGBA(dx+x*4+sx, dy+y*4+sy, pixel)
					}
				}
			}
		}
	}
	for i := 32; i < len(noise)-3; i += 4 {
		x, y := int(noise[i])*300/256, int(noise[i+1])*120/256
		img.SetRGBA(x, y, color.RGBA{noise[i+2], noise[i+3], 100, 255})
	}
	var output bytes.Buffer
	if err := png.Encode(&output, img); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}
