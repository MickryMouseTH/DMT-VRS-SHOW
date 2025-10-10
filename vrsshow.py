import sys
import threading
from datetime import datetime, timedelta
from loguru import logger
import cv2 as cv
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from LogLibrary import Load_Config, Loguru_Logging
import os
import json
import time
import argparse
import psutil
from urllib.parse import quote

# ----------------------- Configuration Values -----------------------
Program_Name = "VRS_Show"      # Program name for identification and logging.
Program_Version = "1.3"      # Program version used for file naming and logging.
# ---------------------------------------------------------------------


if getattr(sys, 'frozen', False):
    script_dir = os.path.dirname(sys.executable)
else:
    script_dir = os.path.dirname(os.path.abspath(__file__))

# Define the lock file name in script_dir (must be after script_dir is defined)
LOCK_FILE = os.path.join(script_dir, f"{Program_Name}.lock")


default_config = {
    "plaza_nvr_ip":"",
    "plaza_nvr_user":"",
    "plaza_nvr_password":"",
    "plaza_to_channel": {
        "DM35": 1,
        "DM36": 2,
        "TB21": 21,
        "TB22": 22
    },
    "dc_nvr_ip": "",
    "lane_to_uid": {
        "AN01": "a1b2c3d4-e5f6-...",
        "AN02": "b2c3d4e5-f6a7-...",
        "TC01": "c3d4e5f6-a7b8-..."
    },
    "log_Level": "DEBUG",
    "Log_Console": 1,      # Set to "true" to enable console logging.
    "log_Backup": 90,      # Log retention duration (number of backup files).
    "Log_Size": "10 MB"    # Maximum log file size before rotation.
}

config = Load_Config(default_config, Program_Name)
logger = Loguru_Logging(config, Program_Name, Program_Version)

def putText_Thai(image, text, position, font_path, font_size, color):
    """
    Function to draw Thai text on an OpenCV image.
    """
    # Convert OpenCV image (NumPy array) to Pillow image.
    pil_image = Image.fromarray(cv.cvtColor(image, cv.COLOR_BGR2RGB))
    
    # Create a drawing object.
    draw = ImageDraw.Draw(pil_image)
    
    # Load the .ttf font.
    font = ImageFont.truetype(font_path, font_size)
    
    # Draw the text on the image.
    draw.text(position, text, font=font, fill=color)
    
    # Convert the Pillow image back to an OpenCV image.
    cv_image = cv.cvtColor(np.array(pil_image), cv.COLOR_RGB2BGR)
    
    return cv_image

def getplaytime(dt):
    """
    Converts local time to Zulu Time (UTC) format for use in a URL.
    """
    logger.debug(f"Converting playtime. Original (Local): {dt.strftime('%Y-%m-%d %H:%M:%S')}")
    delta_7 = timedelta(hours=7)
    dt_utc = dt - delta_7
    converted_date_zulu = dt_utc.strftime("%Y%m%dT%H%M%SZ")
    logger.debug(f"Converted to Zulu Time (UTC): {converted_date_zulu}")
    return converted_date_zulu

# --- Threading Section ---

vcap = None
is_connected = False
thread_error = None

def connect_stream(rtsp_url):
    """
    Function to connect to the RTSP Stream, which runs in a background thread.
    """
    global vcap, is_connected, thread_error
    
    logger.info(f"Thread: Starting connection attempt to URL...")
    try:
        vcap = cv.VideoCapture(rtsp_url)
        if vcap.isOpened():
            logger.debug("Thread: Connection successful. Stream is opened.")
            is_connected = True
        else:
            thread_error = "vcap.isOpened() returned False"
            logger.error(f"Thread: Failed to open stream. {thread_error}")
            is_connected = False
            
    except Exception as e:
        thread_error = str(e)
        logger.error(f"Thread: An exception occurred during connection: {thread_error}")
        is_connected = False

def Get_DC_Nvr(config, script_dir):
    logger.info("🔄 Switching to DC NVR...")
    if len(sys.argv) <= 2:
        logger.error("No arguments provided for DC NVR.")
        return

    # --- Read Config ---
    ip_address = config.get("dc_nvr_ip", "127.0.0.1") # Use IP from config
    lane_to_uid_map = config.get("lane_to_uid", {})

    param_lanename = sys.argv[1]
    param_playtime = sys.argv[2]
    
    uid = lane_to_uid_map.get(param_lanename)
    # Check if UID was found in the config
    if not uid:
        logger.error(f"FATAL: lane_name '{param_lanename}' not found in the config.json")
        return
    
    logger.info(f"Arguments received: lane_name='{param_lanename}', playtime='{param_playtime}'")
    playtime_obj = datetime.strptime(param_playtime, '%d%m%Y%H%M%S')
    playtime_str = getplaytime(playtime_obj)
    
    rtsp_url = f"rtsp://{ip_address}/recordstream?streamid={uid}&playtime={playtime_str}"
    logger.debug(f"Constructed RTSP URL : {rtsp_url}")

    connection_thread = threading.Thread(target=connect_stream, args=(rtsp_url,), daemon=True)
    logger.info("Main: Starting connection thread.")
    connection_thread.start()

    logger.info("Main: Entering loading screen loop while waiting for connection.")

    spinner_angle = 0
    font_path = os.path.join(script_dir, 'THSarabun.ttf')

    while not is_connected and connection_thread.is_alive():
        loading_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        
        center_x, center_y = 320, 200
        radius = 25
        spinner_color = (200, 200, 200) # Light gray
        thickness = 4

        cv.ellipse(loading_frame, (center_x, center_y), (radius, radius), 0, spinner_angle, spinner_angle + 270, spinner_color, thickness)
        spinner_angle = (spinner_angle + 20) % 360

        loading_frame = putText_Thai(
            image=loading_frame,
            text="กำลังโหลดข้อมูล กรุณารอสักครู่",
            position=(180, 250),
            font_path=font_path,
            font_size=28,
            color=(255, 255, 255)
        )
        
        cv.imshow(window_name, loading_frame)
        
        key = cv.waitKey(100) & 0xFF
        if key == ord('q'):
            logger.info("Main: 'q' pressed during loading. Exiting program.")
            cv.destroyAllWindows()
            return
        
        try:
            if cv.getWindowProperty(window_name, cv.WND_PROP_VISIBLE) < 1:
                logger.info("Main: Exited loading loop by user.")
                break
        except cv.error:
            logger.warning("Loading window was destroyed unexpectedly.")
            break
            
    logger.info("Main: Exited loading loop.")
    cv.destroyWindow(window_name)

    if not is_connected or not vcap.isOpened():
        logger.error("Main: Connection failed.")
        if thread_error:
            logger.error(f"Main: Reason: {thread_error}")
        logger.info("================ Program End ==================")
        return

    start_time = time.time()
    duration_seconds = 300 # 5 minutes = 300 seconds
    
    logger.info("Main: Connection successful. Starting video display loop.")
    while True:
        elapsed_time = time.time() - start_time
        if elapsed_time > duration_seconds:
            logger.info(f"Playback stopped automatically after {duration_seconds / 60} minutes.")
            break

        ret, frame = vcap.read()
        if not ret:
            logger.error("Main: Stream ended or failed to read frame.")
            break

        cv.imshow(window_name, frame)

        if cv.waitKey(1) & 0xFF == ord('q'):
            logger.info("Main: 'q' pressed during video playback. Closing stream.")
            break
        
        try:
            if cv.getWindowProperty(window_name, cv.WND_PROP_VISIBLE) < 1:
                logger.info("Main: Video window was closed by user.")
                break
        except cv.error:
            logger.error("Main: Video window was closed, causing cv.error.")
            break

    vcap.release()
    logger.info("Main: Video capture released.")
    cv.destroyAllWindows()
    logger.info("Main: All windows destroyed.")
    logger.info("================ Program End ==================")


def Get_Plaza_Nvr(config, script_dir):
    parser = argparse.ArgumentParser(description="NVR Playback Tool")
    parser.add_argument("plaza", help="Plaza name (e.g., DM35)")
    parser.add_argument("time", help="Start time 'DDMMYYYYHHMMSS'")
    parser.add_argument("duration", nargs='?', type=int, default=500, help="Video duration (seconds)")
    args = parser.parse_args()
    plaza_map = config.get("plaza_to_channel", {})
    plaza_nvr_ip = config.get("plaza_nvr_ip")
    plaza_nvr_user = config.get("plaza_nvr_user")
    plaza_nvr_password = config.get("plaza_nvr_password")

    channel_no = plaza_map.get(args.plaza.upper())
    
    if channel_no is None:
        logger.error(f"Error: Plaza name '{args.plaza}' not found.")
        return

    try:
        start_time_obj = datetime.strptime(args.time, "%d%m%Y%H%M%S")
        end_time_obj = start_time_obj + timedelta(seconds=args.duration)
        starttime_str = start_time_obj.strftime("%Y_%m_%d_%H_%M_%S")
        endtime_str = end_time_obj.strftime("%Y_%m_%d_%H_%M_%S")
    except ValueError:
        logger.error("Error: Invalid time format. Please use 'DDMMYYYYHHMMSS'.")
        return

    rtsp_url = (
        f"rtsp://{plaza_nvr_user}:{plaza_nvr_password}@{plaza_nvr_ip}:554/cam/playback?"
        f"channel={channel_no}&"
        f"starttime={starttime_str}&"
        f"endtime={endtime_str}"
    )

    logger.info(f"Connecting to Plaza NVR (CH: {channel_no}) with a 3-second timeout...")
    logger.debug(f"URL: {rtsp_url}")

    cap_container = []
    def connect_task(url, container):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        cap_thread = cv.VideoCapture(url)
        if cap_thread.isOpened():
            container.append(cap_thread)

    connection_thread = threading.Thread(target=connect_task, args=(rtsp_url, cap_container), daemon=True)
    connection_thread.start()

    logger.info("Displaying loading screen... waiting for connection.")
    font_path = os.path.join(script_dir, 'THSarabun.ttf')
    timeout_seconds = 5.0
    start_time = datetime.now()
    
    spinner_angle = 0

    while connection_thread.is_alive():
        elapsed_time = (datetime.now() - start_time).total_seconds()
        if elapsed_time > timeout_seconds:
            break

        loading_frame = np.zeros((480, 640, 3), dtype=np.uint8)

        center_x, center_y = 320, 200
        radius = 25
        spinner_color = (200, 200, 200)
        thickness = 4
        cv.ellipse(loading_frame, (center_x, center_y), (radius, radius), 0, spinner_angle, spinner_angle + 270, spinner_color, thickness)
        spinner_angle = (spinner_angle + 20) % 360
        
        loading_frame = putText_Thai(
            image=loading_frame,
            text="กำลังโหลดข้อมูล กรุณารอสักครู่",
            position=(180, 250),
            font_path=font_path,
            font_size=28,
            color=(255, 255, 255)
        )
        cv.imshow(window_name, loading_frame)
        
        key = cv.waitKey(100) & 0xFF
        if key == ord('q') or cv.getWindowProperty(window_name, cv.WND_PROP_VISIBLE) < 1:
            logger.warning("User cancelled during loading...")
            break

    logger.info("Finished displaying loading screen.")
    cv.destroyWindow(window_name)

    if connection_thread.is_alive():
        logger.error(f"Timeout: Could not connect to NVR within {timeout_seconds} seconds.")
        return

    if not cap_container:
        logger.error("Error: Connection successful, but failed to open stream (isOpened failed).")
        return
    
    cap = cap_container[0]
    logger.info("Connection to NVR successful!")
    playback_successful = True 
    
    video_window_name = f"Playback - Plaza {args.plaza} (CH {channel_no})"


    while True:
        ret, frame = cap.read()
        if not ret:
            logger.info("Video has ended or the connection was lost.")
            break
            
        cv.imshow(video_window_name, frame)
        
        key = cv.waitKey(1) & 0xFF
        if key == ord('q') or cv.getWindowProperty(video_window_name, cv.WND_PROP_VISIBLE) < 1:
            if key == ord('q'):
                logger.info("User pressed 'q', closing the program...")
            else:
                logger.info("Window closed by user.")
            break

    cap.release()
    cv.destroyAllWindows()
    logger.info("================ Program End ==================")
    return playback_successful


def handle_singleton_lock():
    """Checks and handles singleton instance to allow only one instance to run."""
    logger.info("Checking for duplicate instances...")
    
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read())
            
            if psutil.pid_exists(old_pid):
                logger.warning(f"Found a hanging instance of {Program_Name} (PID: {old_pid}). Terminating it...")
                try:
                    p = psutil.Process(old_pid)
                    p.kill()
                    p.wait()
                    logger.info(f"Old instance (PID: {old_pid}) has been terminated.")
                except psutil.NoSuchProcess:
                    logger.info("Old instance terminated itself before it could be killed.")
                except Exception as e:
                    logger.error(f"An error occurred while terminating the old instance: {e}")
            else:
                logger.info("Found a stale lock file, but the process is not running. (Likely from an improper shutdown).")

        except (ValueError, FileNotFoundError):
            logger.warning("Lock file is corrupted or could not be read. A new one will be created.")
        
        os.remove(LOCK_FILE)

    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Lock file created successfully (PID: {os.getpid()}).")
    except Exception as e:
        logger.error(f"Could not create lock file: {e}")

window_name = f'{Program_Name} Version {Program_Version}'

if __name__ == "__main__":
    try:
        config = Load_Config(Program_Name,script_dir)
        Loguru_Logging(config,script_dir)
        logger.info(f"================ Program {Program_Name} Version {Program_Version} Start ================")

        handle_singleton_lock()
        

        plaza_success = Get_Plaza_Nvr(config, script_dir)
        
        if not plaza_success:
            Get_DC_Nvr(config, script_dir)

    except Exception as e:
        logger.error(f"Main Program Error : {e}")

    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logger.info("Lock file removed. Program has exited cleanly.")