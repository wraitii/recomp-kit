-- good.lua - registers a simple event callback through the curated bindings.
seen_turns = 0
pop.log("good.lua loaded for " .. pop.mod_id())

pop.on_turn("before", function()
  seen_turns = seen_turns + 1
end)

pop.on_level_load(function() pop.log("level loaded") end)
