def q(s) = string.quote(s) end

def on_start_meta(m) =
  artist = if m["artist"] != "" then m["artist"] else "" end
  title  = if m["title"]  != "" then m["title"]  else "" end
  file   = if m["filename"] != "" then m["filename"] else "" end

  log("ON-AIR: #{artist} - #{title} (#{file})")

  ignore(process.run(
    "/usr/bin/python3 /usr/local/bin/ls_radio.py track-start "
    ^ "--artist " ^ q(artist) ^ " "
    ^ "--title "  ^ q(title)  ^ " "
    ^ "--path "   ^ q(file)
  ))
  m
end

def next_request() =
  uri = string.trim(process.read("/usr/bin/python3 /usr/local/bin/ls_radio.py pick-next"))
  if uri == "" then
    request.create("/usr/share/liquidsoap/_silence.mp3")
  else
    request.create(uri)
  end
end

radio = request.dynamic(next_request)
radio = map_metadata(on_start_meta, radio)

radio = mksafe(crossfade(radio))

radio = normalize(radio, target=-16.0, threshold=-22.0, window=0.5)

radio = compress(radio,
  threshold=-18.0,    # Higher threshold = less aggressive
  ratio=2.5,          # Gentler ratio
  attack=0.01,        # Slightly slower attack
  release=0.3,        # Longer release to avoid pumping
  gain=3.0            # Make up gain
)

radio = limit(radio, threshold=-0.5, attack=0.005, release=0.1)

output.icecast(
  %mp3(bitrate=192),
  host="stream.example.com", port=8000, password="",
  mount="/live", name="A Radio Stream",
  url="https://radio.example.com/radio", genre="Various", public=true,
  radio
)
