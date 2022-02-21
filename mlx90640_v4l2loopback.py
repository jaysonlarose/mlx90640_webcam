#!/usr/bin/env python3

# MLX90640 remote transport script
# by Jayson Larose (jayson@interlaced.org)

# Dependencies:
# * v4l2 python library (https://github.com/fnoop/python-v4l2.git)
# * v4l2loopback kernel module (https://github.com/umlaeute/v4l2loopback)

# PIP dependencies:
# * ansicolor

# Ubuntu package dependencies:
# * gir1.2-gudev-1.0
# * libgudev-1.0-0
# * gir1.2-glib-2.0
# * ffmpeg

# NOTE: At the time of writing this (2022-02-21), the version of v4l2loopback
# shipped with Ubuntu has some serious problems, and isn't supported.
# Use the version from github. Trust me, it's better.

# This script automates the process of pulling MLX90640 frames from a
# Raspberry Pi via ssh and feeding it into a v4l2loopback, so it can be
# used as a webcam device, OBS source, et cetera.
# You'll need an SSH host key set up, as this script probably won't handle
# a password prompt very well.

import os, sys, fcntl, subprocess, time, fcntl, gi, collections, decimal
gi.require_version('GUdev', '1.0')
from gi.repository import GUdev

VideoDevice = collections.namedtuple("VideoDevice", ['path', 'name', 'driver', 'caps', 'gudev_obj', 'v4l2caps_obj'])
def gimmie_video_devices(gudev_client):# {{{
	try:
		import v4l2
	except ImportError:
		print("v4l2 module not found!", file=sys.stderr)
		print("try: sudo pip3 install git+https://github.com/fnoop/python-v4l2", file=sys.stderr)
		sys.exit(1)
	devs = gudev_client.query_by_subsystem("video4linux")
	ret = []
	for d in devs:
		devpath = d.get_device_file()
		if devpath is None:
			continue
		vd = open(devpath, "rb")
		cp = v4l2.v4l2_capability()
		fcntl.ioctl(vd, v4l2.VIDIOC_QUERYCAP, cp)
		vd.close()
		dd = dict([ [x, d.get_property(x)] for x in d.get_property_keys() ])
		capset = set([ x for x in dd['ID_V4L_CAPABILITIES'].split(':') if len(x) > 0 ])
		if 'ID_V4L_PRODUCT' not in dd:
			continue
		kwparms = {
			'path': devpath,
			'name': cp.card.decode(),
			'driver': cp.driver.decode(),
			'caps': capset,
			'gudev_obj': d,
			'v4l2caps_obj': cp,
		}
		ret.append(VideoDevice(**kwparms))
	return ret
# }}}
def get_fractional_framerate(fr_string):# {{{
	# make a Decimal out of it
	frd = decimal.Decimal(fr_string)
	a, b = frd.as_integer_ratio()
	return f"{a}/{b}"
# }}}
def parse_ffmpegline(data):# {{{
	"""
	Take a `bytes()` sequence what looks like thus:

		frame=17703 fps=478 q=-0.0 size=   30184kB time=00:09:50.62 bitrate= 418.7kbits/s speed=15.9x

	And return its constituent key/value pairs as a `str`:`str` dict.

	Returns None if parsing fails.
	"""
	if isinstance(data, bytes):
		data = data.decode()
	# First, split by '=' and trim off any leading/trailing whitespace.
	# This gives us something like this:
	# ['frame', '17703 fps', '478 q', '-0.0 size', '30184kB time', '00:09:50.62 bitrate', '418.7kbits/s speed', '15.9x']
	frags = [ x.strip() for x in data.split('=') ]
	frag_qty = len(frags)
	if frag_qty < 2:
		return None
	ret = dict()
	for i, x in enumerate(frags):
		# Now, iterate through each fragment. Treat the first fragment
		# special, and directly call it "key". For each subsequent fragment
		# EXCEPT the last one, split it by space (should always result in two
		# subfragments).  Pair the first subfragment up with "key" and add it
		# to the return dict. Then call the second subfragment the new "key"
		# and proceed with the next iteration. Finally, pair the final
		# fragment up with the last "key".
		if i == 0:
			k = x
		elif i == frag_qty - 1:
			ret[k] = x
		else:
			subfrags = x.split(' ')
			if len(subfrags) != 2:
				return None
			ret[k] = subfrags[0]
			k = subfrags[1]
	if 'frame' not in ret:
		return None
	return ret
# }}}

FRAME_RATE     = 32
REMOTE_HOST    = 'thermopi'
REMOTE_SCRIPT  = '/home/common/RaspberryPi/projects/mlx90640/mlx90640_pil.py'
REMOTE_ARGS    = "{remote_script} -c -r REFRESH_{rate}_HZ -n -q"
LOOPBACK_LABEL = "MLX90640"
LOOPBACK_PATH  = "/dev/video11"
FILTER_PARMS   = "transpose=0,scale=iw*4:ih*4:flags=neighbor"


def make_nonblocking(fd):
	fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


if __name__ == '__main__':
	import argparse
	parser = argparse.ArgumentParser()
	parser.set_defaults(
		host           = REMOTE_HOST,
		rate           = FRAME_RATE,
		loopback_label = LOOPBACK_LABEL,
		loopback_path  =  LOOPBACK_PATH,
		remote_script  = REMOTE_SCRIPT,
		remote_args    = REMOTE_ARGS,
		filter_parms   = FILTER_PARMS,
		run            = None
	)
	for argname in parser._defaults.keys():
		arg_flag = argname.replace('_', '-')
		arg_type = str
		if isinstance(parser.get_default(argname), int):
			arg_type = int
		parser.add_argument(f"--{arg_flag}", action="store", type=arg_type, help=f"(default: {parser.get_default(argname)!r})")
	parser.add_argument("--stdout", action="store_true", default=False, dest="output_stdout", help="Bypass ffmpeg, output raw frames to stdout")
	parser.add_argument("--no-v4l2", action="store_true", default=False, dest="no_v4l2", help="Don't do anything with v4l2loopback-ctl")
	parser.add_argument("--quiet", action="store_true", default=False, dest="quiet", help="shhh")
	args = parser.parse_args()
	if not args.no_v4l2:
		gudev_client = GUdev.Client()
		loopback_devs = [ x for x in gimmie_video_devices(gudev_client) if x.driver == 'v4l2 loopback' ]
		matches = [ x for x in loopback_devs if x.name == args.loopback_label ]
		if len(matches) > 0:
			tgt = matches[0]
			procargs = ['v4l2loopback-ctl', 'del', tgt.path]
			subprocess.run(procargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

		procargs = ['v4l2loopback-ctl', 'add', '-n', args.loopback_label, '-x', '1', args.loopback_path]
		proc = subprocess.run(procargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
		loopback_path = proc.stdout.decode().splitlines()[0]

		
		tries = 20
		while True:
			try:
				procargs = ['v4l2loopback-ctl', 'set-fps', loopback_path, get_fractional_framerate(args.rate)]
				proc = subprocess.run(procargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
				break
			except subprocess.CalledProcessError:
				if tries <= 0:
					raise
			tries -= 1
			time.sleep(0.05)

	procargs = ['ssh', args.host, args.remote_args.format(**args.__dict__)]
	print(f"Running {procargs}", file=sys.stderr)
	prockwargs = {}
	if not args.output_stdout:
		prockwargs['stdout'] = subprocess.PIPE
	xferproc = subprocess.Popen(procargs, **prockwargs)

	if not args.output_stdout:
		make_nonblocking(xferproc.stdout)
		if args.run is None:
			procargs = ['ffmpeg', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s:v', '32x24', '-r', f"{args.rate}", '-i', '-', '-vf', args.filter_parms, '-f', 'v4l2', loopback_path]
			if args.quiet:
				procargs = procargs[:1] + ['-loglevel', 'error'] + procargs[1:]
		else:
			import shlex
			procargs = shlex.split(args.run)
		print(f"Running {procargs}", file=sys.stderr)
		prockwargs = {}
		prockwargs['stdin'] = subprocess.PIPE
		if not args.quiet:
			prockwargs['stderr'] = subprocess.PIPE
		mpegproc = subprocess.Popen(procargs, **prockwargs)

		if not args.quiet:
			make_nonblocking(mpegproc.stderr)
			select_inputs = [xferproc.stdout, mpegproc.stderr]
			class MpegprocStderrHandler:
				def __init__(self, msg=None):
					self.buf = b''
					self.msg = msg
					self.msg_printed = False
				def feed(self, data):
					import ansicolor
					if len(data) == 0:
						sys.exit(1)
					self.buf += data
					lines = self.buf.splitlines(keepends=True)
					if lines[-1][-1] not in [b'\r', b'\n']:
						self.buf = lines.pop()
					else:
						self.buf = b''
					if len(lines) > 0:
						for line in [ ansicolor.strip_escapes(x.rstrip(b"\r\n").decode()) for x in lines ]:
							progress_dict = parse_ffmpegline(line)
							if progress_dict is not None:
								if self.msg is not None:
									if not self.msg_printed:
										self.msg_printed = True
										print(self.msg, file=sys.stderr)
								sys.stderr.write(f"{progress_dict['frame']} {progress_dict['fps']}fps\r")
								sys.stderr.flush()
							else:
								print(line, file=sys.stderr)
			handler = MpegprocStderrHandler(msg=f"gst-launch-1.0 v4l2src device={loopback_path} ! videoconvert ! autovideosink sync=false")
		else:
			select_inputs = [xferproc.stdout]

		import select
		while True:
			ifh, ofh, xfh = select.select(select_inputs, [], [])
			if xferproc.stdout in ifh:
				mpegproc.stdin.write(xferproc.stdout.read())
			if not args.quiet and mpegproc.stderr in ifh:
				handler.feed(mpegproc.stderr.read())
