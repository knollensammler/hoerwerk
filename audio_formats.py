"""The output suffix, audio codec and muxer are chosen together."""
FORMATS = {
    "MP3": (".mp3", ("-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3")),
    "M4B": (".m4b", ("-codec:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-f", "mp4")),
    "FLAC": (".flac", ("-codec:a", "flac", "-f", "flac")),
}
