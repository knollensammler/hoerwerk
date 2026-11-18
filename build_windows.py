"""Compatibility entry point for native Windows x64 builds."""
from build_release import main


if __name__ == "__main__":
    main(required_target="windows")
