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

radio = mksafe(
  crossfade(
    duration     = 5.0,
    fade_in      = 2.5,
    fade_out     = 2.5,
    smart        = false,
    conservative = true,
    radio
  )
)

radio = normalize(
  radio,
  target   = -18.0,
  threshold= -40.0,
  window   = 2.0,
  gain_max = 6.0,
  gain_min = -6.0
)

radio = compress(
  radio,
  threshold = -14.0,
  ratio     = 1.8,
  attack    = 0.02,
  release   = 0.25,
  gain      = 0.0
)

radio = limit(
  radio,
  threshold = -1.0,
  attack    = 0.005,
  release   = 0.1
)

output.icecast(
  %mp3(bitrate=192),
  host="stream.example.com", port=8000, password="",
  mount="/live", name="A Radio Stream",
  url="https://radio.example.com/radio", genre="Various", public=true,
  radio
)
