# Settings for dmgbuild — used by scripts/build_app.py
import os

application = os.environ["FACEDICT_APP"]
guide = os.environ["FACEDICT_GUIDE"]
appname = os.path.basename(application)

format = "UDZO"                 # compressed, read-only
size = None                     # dmgbuild works it out from the contents

files = [application, (guide, "Get Started.md")]
symlinks = {"Applications": "/Applications"}
icon_locations = {appname: (150, 170), "Applications": (450, 170), "Get Started.md": (300, 350)}

window_rect = ((200, 180), (620, 500))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
arrange_by = None
grid_offset = (0, 0)
grid_spacing = 100
scroll_position = (0, 0)
label_pos = "bottom"
text_size = 13
icon_size = 128
