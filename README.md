# Scene Analysis and Object Detection System

This project uses computer vision to detect and track objects and people in real-time, storing the information for later retrieval.

## Features
- Real-time object and person detection using YOLOv8
- Automatic scene information logging
- Database storage of detected objects with timestamps
- Query interface for retrieving historical scene information

## Setup

1. Install the required dependencies:
```bash
pip install -r requirements.txt
```

2. Download the YOLOv8 model (this will happen automatically on first run)

## Usage

1. To start the scene analyzer and begin detecting objects:
```bash
python scene_analyzer.py
```
This will open your camera and start detecting objects in real-time. Press 'q' to quit.

2. To query stored scene information:
```bash
python query_scene.py
```
This will open an interactive interface where you can:
- Search for specific objects
- View all detected objects
- See when objects were present in the scene

## How it Works

1. The system captures video frames from your camera
2. Each frame is analyzed using YOLOv8 for object detection
3. Detected objects are stored in a SQLite database with timestamps
4. You can query the database to find when specific objects were present

## Notes

- The system uses YOLOv8 which can detect 80 different types of common objects
- Detection results are stored in `scene_data.db`
- The confidence threshold can be adjusted in the code if needed
# Mark1
