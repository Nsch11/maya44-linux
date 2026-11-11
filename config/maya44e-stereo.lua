-- SPDX-License-Identifier: GPL-3.0-or-later
-- The same passive loopback arrangement used by WirePlumber's SplitPCM.
-- Only the shared ALSA parent negotiates a sample rate and hardware period.
-- The module binds only the three exact supported PCI subsystem IDs.
-- Match its actual PCM rather than a machine-specific PCI slot.
-- Keep these in the script's global table for its entire lifetime. Local
-- references would let the Lua GC discard the object manager after startup.
modules = {}
om = ObjectManager {
  Interest {
    type = "node",
    Constraint { "alsa.driver_name", "=", "snd_maya44e", type = "pw" },
    Constraint { "device.profile.name", "=", "multichannel-output", type = "pw" },
    Constraint { "api.alsa.pcm.stream", "=", "playback", type = "pw" },
  }
}

local function stereo_pair(node, stem, suffix, description, positions, priority)
  local sink_name = stem .. "_output_" .. suffix
  local args = Json.Object {
    ["capture.props"] = Json.Object {
      ["node.name"] = sink_name,
      ["node.description"] = description,
      ["node.nick"] = description,
      ["media.class"] = "Audio/Sink",
      ["audio.position"] = Json.Array { "FL", "FR" },
      ["device.id"] = node.properties["device.id"],
      ["device.icon-name"] = "audio-card-analog",
      ["node.virtual"] = false,
      ["priority.session"] = priority,
      ["maya44e.stereo-pair"] = suffix,
    },
    ["playback.props"] = Json.Object {
      ["node.name"] = sink_name .. ".playback",
      ["node.description"] = description .. " → PCM",
      ["media.class"] = "Stream/Output/Audio/Internal",
      ["audio.position"] = Json.Array (positions),
      ["target.object"] = node.properties["node.name"],
      ["stream.dont-remix"] = true,
      ["node.passive"] = true,
      ["node.dont-fallback"] = true,
      ["node.dont-reconnect"] = true,
      ["state.restore-props"] = false,
    },
  }
  return LocalModule("libpipewire-module-loopback", args:get_data(), {})
end

om:connect("object-added", function (_, node)
  local card_id = node.properties["alsa.id"] or ""
  local base = card_id:gsub("_%d+$", "")
  local names = { MAYA44e = "MAYA44e", MAYA44XTe = "MAYA44 XTe", MAYA44eX = "MAYA44 eX" }
  local description = names[base]
  if not description then return end
  if base ~= card_id then description = description .. " (" .. card_id .. ")" end
  local stem = card_id:lower()
  modules[node["bound-id"]] = {
    stereo_pair(node, stem, "12", description .. " 1/2", { "FL", "FR" }, 1200),
    stereo_pair(node, stem, "34", description .. " 3/4", { "RL", "RR" }, 1190),
  }
end)
om:connect("object-removed", function (_, node)
  -- dont-reconnect tears down the streams when the parent disappears.
  -- Release the local module references too (also works before WP 0.5.16).
  modules[node["bound-id"]] = nil
end)
om:activate()
