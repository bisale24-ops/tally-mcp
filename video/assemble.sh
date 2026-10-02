#!/usr/bin/env bash
# Join the narrated cards and the live recording into the submission video.
#   video/assemble.sh   ->  video/build/tally.mp4
set -euo pipefail
cd "$(dirname "$0")/build"
ffmpeg -y -loglevel error -i intro.mp4 -i demo.mp4 -i outro.mp4 -filter_complex \
  "[0:v]fps=30,format=yuv420p,setsar=1[v0];[1:v]fps=30,format=yuv420p,setsar=1[v1];[2:v]fps=30,format=yuv420p,setsar=1[v2];\
   [0:a]aresample=48000,aformat=channel_layouts=stereo[a0];[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.45[a1];[2:a]aresample=48000,aformat=channel_layouts=stereo[a2];\
   [v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]" \
  -map "[v]" -map "[a]" -c:v libx264 -crf 21 -preset medium -c:a aac -b:a 160k -movflags +faststart tally.mp4
ffprobe -v error -show_entries format=duration,size -of default=nw=1 tally.mp4
