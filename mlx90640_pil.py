#!/usr/bin/env python3

# MLX90640 data streaming script.
# Portions by Jayson Larose (jayson@interlaced.org)
# Portions by Limor Fried, covered by MIT license:

# SPDX-FileCopyrightText: 2021 ladyada for Adafruit Industries
# SPDX-License-Identifier: MIT

# This is a highly modified version of mlx90640_pil.py, found in
# https://github.com/adafruit/Adafruit_CircuitPython_MLX90640

# Dependencies:
# * numpy
# * smbus2 (https://github.com/kplindegaard/smbus2)

# Additional dependencies, mostly for pretty-printing peformance data:
# * jlib (https://github.com/jaysonlarose/jlib)
# * JaysTerm (https://github.com/jaysonlarose/JaysTerm)

# Helpful hint:
# The above GitHub-hosted dependencies can be easily installed from pip3!
# Try: sudo pip3 install git+https://github.com/blahblah

import math
from PIL import Image
import smbus2
import mlx90640
import io, os, sys, struct, time, jlib, numpy

FILENAME = "mlx.jpg"

MINTEMP = 25.0	# low range of the sensor (deg C)
MAXTEMP = 45.0	# high range of the sensor (deg C)
COLORDEPTH = 1000  # how many color values we can have
#INTERPOLATE = 10  # scale factor for final image
INTERPOLATE = 1  # scale factor for final image
TEMP_SKEW = 0.00

def floatify(*heatmap):
	return tuple([ tuple([x[0], tuple([ y / 255 for y in x[1] ])]) for x in heatmap ])

# the list of colors we can choose from
heatmaps = {
	'classic': (
		( 0.0, (0, 0, 0)),
		(0.20, (0, 0, 0.5)),
		(0.40, (0, 0.5, 0)),
		(0.60, (0.5, 0, 0)),
		(0.80, (0.75, 0.75, 0)),
		(0.90, (1.0, 0.75, 0)),
		(1.00, (1.0, 1.0, 1.0)),
	),
	'grayscale': (
		( 0.0, (0, 0, 0)),
		(1.00, (1.0, 1.0, 1.0)),
	),
		'enby': floatify(
		(0.00, (44, 44, 44)),
		(0.3333333333333, (252, 244, 52)),
		(0.6666666666666, (252, 252, 252)),
		(1.00, (156, 89, 209)),
	),
}

# some utility functions
def constrain(val, min_val, max_val):
	return min(max_val, max(min_val, val))


def map_value(x, in_min, in_max, out_min, out_max):
	#print(f"map_value({x}, {in_min}, {out_min}, {out_max}", file=sys.stderr)
	return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


def gaussian(x, a, b, c, d=0):
	return a * math.exp(-((x - b) ** 2) / (2 * c ** 2)) + d


def gradient(x, width, cmap, spread=1):
	width = float(width)
	r = sum(
		[gaussian(x, p[1][0], p[0] * width, width / (spread * len(cmap))) for p in cmap]
	)
	g = sum(
		[gaussian(x, p[1][1], p[0] * width, width / (spread * len(cmap))) for p in cmap]
	)
	b = sum(
		[gaussian(x, p[1][2], p[0] * width, width / (spread * len(cmap))) for p in cmap]
	)
	r = int(constrain(r * 255, 0, 255))
	g = int(constrain(g * 255, 0, 255))
	b = int(constrain(b * 255, 0, 255))
	return r, g, b

def do_the_skew(cur_val, tgt_val, max_skew, magfactor):
	"""
	moves cur_val a little closer to tgt_val.
	"""
	#print(f"do_the_skew({cur_val}, {tgt_val}, {max_skew}, {magfactor})", file=sys.stderr)
	delta = tgt_val - cur_val
	#print(f"delta: {delta}", file=sys.stderr)
	adjust = min([delta, (delta / abs(delta) * max_skew)], key=abs)
	#print(f"adjust: {adjust}", file=sys.stderr)
	return cur_val + (adjust * magfactor)

def generate_colormap(colordepth, heatmap):
	colormap = [0] * colordepth
	for i in range(colordepth):
		colormap[i] = gradient(i, colordepth, heatmap)
	return colormap

def do_frame(mlx, framebuf):
	# get sensor data
	success = False
	while not success:
		try:
			mlx.getField(framebuf)
			success = True
		except ValueError:
			continue


def do_pixels(framebuf, colormap, mintemp, maxtemp):

	# create the image
	pixels = [0] * 768
	for i, pixel in enumerate(framebuf):
		coloridx = map_value(pixel, mintemp, maxtemp, 0, len(colormap) - 1)
		coloridx = int(constrain(coloridx, 0, len(colormap) - 1))
		pixels[i] = colormap[coloridx]
	
	return pixels

def do_img(pixels):

	# save to file
	img = Image.new("RGB", (32, 24))
	img.putdata(pixels)
	img = img.transpose(Image.FLIP_TOP_BOTTOM)
	img = img.resize((32 * INTERPOLATE, 24 * INTERPOLATE), Image.BICUBIC)
	return img

if __name__ == '__main__':
	import argparse
	parser = argparse.ArgumentParser()
	parser.add_argument("-c", action="store_true", dest="continual", default=False, help="Output raw RGB24 frames continually to STDOUT")
	parser.add_argument("-r", action="store", dest="rate", default='REFRESH_1_HZ', help="Set continual mode refresh rate. Default: REFRESH_1_HZ")
	parser.add_argument("-n", action="store_true", dest="continual_headerless", default=False, help="Do not output frame size/type header")
	parser.add_argument("-q", action="store_true", dest="continual_quiet", default=False, help="Do not output performance data on STDERR")
	parser.add_argument("-d", action="store_true", dest="dump", default=False, help="Output color-coded temperature data for a single frame")
	parser.add_argument("--heatmap", action="store", dest="heatmap", default="classic", help="Define which heatmap to apply when rendering pixels")
	parser.add_argument("--temp-skew", action="store", dest="temp_skew", default=TEMP_SKEW, type=float, help="Modify temperature auto-ranging skew rate. Default: {TEMP_SKEW}")
	parser.add_argument("--mintemp", action="store", dest="mintemp", default=MINTEMP, type=float, help=f"Define initial minimum temperature. Default: {MINTEMP}")
	parser.add_argument("--maxtemp", action="store", dest="maxtemp", default=MAXTEMP, type=float, help=f"Define initial maximum temperature. Default: {MAXTEMP}")
	args = parser.parse_args()

	bus = smbus2.SMBus(1)
	mlx = mlx90640.MLX90640(bus)
	framebuf = [0] * 768

	colormap = generate_colormap(COLORDEPTH, heatmaps[args.heatmap])

	if not args.dump:
		if args.continual:
			rateval = getattr(mlx90640.RefreshRate, args.rate)
			mlx.refresh_rate = rateval
			img_mode = 'RAW'
			if not args.continual_headerless:
				sys.stdout.buffer.write(struct.pack(">I", 32))
				sys.stdout.buffer.write(struct.pack(">I", 24))
				sys.stdout.buffer.write(struct.pack(">I", len(img_mode.encode())))
				sys.stdout.buffer.write(img_mode.encode())
			start = time.monotonic()
			cnt = 0
			low_temp = args.mintemp
			high_temp = args.maxtemp
			do_frame(mlx, framebuf)
			while True:
				do_frame(mlx, framebuf)
				t = min(framebuf)
				low_temp = do_the_skew(low_temp, min(framebuf), args.temp_skew, 0.1)
				high_temp = do_the_skew(high_temp, max(framebuf), args.temp_skew, 0.1)
				pixels = do_pixels(framebuf, colormap, low_temp, high_temp)
				if img_mode == 'PNG':
					img = do_img(pixels)
					bio = io.BytesIO()
					img.save(bio, format="png")
					imgbuf = bio.getvalue()
				elif img_mode == 'RAW':
					imgbuf = bytes([ item for sublist in pixels for item in sublist ])
				if not args.continual_headerless:
					sys.stdout.buffer.write(struct.pack(">I", len(imgbuf)))
				sys.stdout.buffer.write(imgbuf)
				sys.stdout.buffer.flush()
				cnt += 1
				if cnt % 10 == 0:
					totaltime = time.monotonic() - start
					if totaltime > 0 and mlx.fields > 0:
						pct = (mlx.i2ctime / totaltime) * 100
						pctxfer = (mlx.i2cxfertime / totaltime) * 100
						wpf = mlx.waitcycles / mlx.fields
						if not args.continual_quiet:
							sys.stderr.write(f"{pct:5.2f}% i2c time, {pctxfer:5.2f}% xfer time, {wpf:5.2f} wait cycles/field low {low_temp:5.2f} high {high_temp:5.2f}\033[0K\r")
							sys.stderr.flush()
		else:
			do_frame(mlx, framebuf)
			do_frame(mlx, framebuf)
			pixels = do_pixels(framebuf, colormap, args.mintemp, args.maxtemp)
			img = do_img(pixels)
			img.save("/home/jayson/mlx.png")

	else:
		if args.continual:
			import JaysTerm
			globals().update(jlib.get_fabulous(autostr=False))
			c_start = JaysTerm.Term.getCursor()
			rateval = getattr(mlx90640.RefreshRate, args.rate)
			mlx.refresh_rate = rateval
			do_frame(mlx, framebuf)
			low_temp = args.mintemp
			high_temp = args.maxtemp
			while True:
				rotval = 1
				JaysTerm.Term.setCursor(1, 1)
				do_frame(mlx, framebuf)
				low_temp = do_the_skew(low_temp, min(framebuf), args.temp_skew, 0.1)
				high_temp = do_the_skew(high_temp, max(framebuf), args.temp_skew, 0.1)
				pixels = do_pixels(framebuf, colormap, low_temp, high_temp)
				farr = numpy.flipud(numpy.rot90(numpy.array(framebuf, dtype=float).reshape(-1, 32), rotval))
				parr = numpy.flipud(numpy.rot90(numpy.array(pixels, dtype=numpy.uint8).reshape(-1, 32, 3), rotval))
				for rownum in range(farr.shape[0]):
					print(" ".join([ str(fgtrue(f"#{parr[rownum][i][0]:02x}{parr[rownum][i][1]:02x}{parr[rownum][i][2]:02x}", f"{farr[rownum][i]:5.2f}")) for i in range(farr.shape[1]) ]) + jlib.encapsulate_ansi('erase_line_from_cursor'))
				sys.stderr.write(jlib.encapsulate_ansi('erase_screen_from_cursor'))
				sys.stderr.flush()
		else:
			do_frame(mlx, framebuf)
			do_frame(mlx, framebuf)
			rows = jlib.splitlen_array(framebuf, 32)
			for row in rows:
				print(" ".join([ f"{x:5.2f}" for x in row ]))
