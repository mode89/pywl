pkgs: with pkgs; [
  (python3.withPackages (ps: with ps; [
    cffi
    setuptools
  ]))
  pkg-config
  wlroots_0_19
  wayland
  wayland-protocols
  wayland-scanner
  libxkbcommon
  pixman
  alacritty
]
