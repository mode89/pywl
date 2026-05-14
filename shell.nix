{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages =
    (import ./deps.nix pkgs) ++
    (with pkgs; [
      xpra
      foot
      gtk4
      gobject-introspection
      python3Packages.pygobject3
    ]);
}
