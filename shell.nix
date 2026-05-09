{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = import ./deps.nix pkgs;
}
