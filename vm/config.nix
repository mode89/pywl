{ config, pkgs, lib, ... }:

{
  system.stateVersion = "24.11";

  # Headed VM: graphical QEMU window with virtio-gpu + spice for decent input/video.
  virtualisation.vmVariant.virtualisation = {
    graphics = true;
    memorySize = 4096;
    cores = 4;
    forwardPorts = [
      { from = "host"; host.port = 2222; guest.port = 22; }
    ];
    # Hardware-accelerated virtio-gpu via virgl (host GPU passthrough for rendering).
    qemu.options = [
      "-device virtio-vga-gl"
      "-display gtk,gl=on,show-cursor=on"
    ];
    # Mount the project read-write into the guest at /mnt/pywl.
    sharedDirectories.pywl = {
      source = toString ./..;
      target = "/mnt/pywl";
    };
  };

  # Auto-login on tty1 so you land straight in a shell.
  services.getty.autologinUser = "dev";

  users.users.dev = {
    isNormalUser = true;
    extraGroups = [ "wheel" "video" "input" ];
    password = "dev";
  };
  users.users.root.password = "root";

  # Tools to run/develop pywl inside the VM.
  environment.systemPackages = (with pkgs; [
    wayland-utils
    mesa-demos
    vim
    tmux
    htop
  ]) ++ (import ../deps.nix pkgs);

  # Useful env so wlroots picks the right backend automatically when run from tty.
  environment.sessionVariables = {
    XDG_RUNTIME_DIR = "/run/user/1000";
    TERM = "alacritty";
  };

  # 3D acceleration in guest.
  hardware.graphics.enable = true;

  fonts.packages = with pkgs; [
    dejavu_fonts
    noto-fonts
    noto-fonts-color-emoji
    nerd-fonts.jetbrains-mono
  ];

  nix.nixPath = [ "nixpkgs=${toString <nixpkgs>}" ];

  networking.hostName = "pywl";
  networking.firewall.enable = false;

  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = true;
    settings.PermitRootLogin = "yes";
  };
}
