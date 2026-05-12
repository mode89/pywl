pkgs: with pkgs; [
  (python3.withPackages (ps: with ps; [
    cffi
    setuptools
    pytest
  ]))
  pkg-config
  wlroots_0_19
  wayland
  wayland-protocols
  wlr-protocols
  wayland-scanner
  libxkbcommon
  pixman
]
