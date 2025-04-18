from flask import Flask, request, jsonify, send_file, session
from flask_session import Session
from werkzeug.utils import secure_filename
import PyPDF2
from ebooklib import epub
from bs4 import BeautifulSoup
import re
import threading
import queue
import time
import copy
from io import BytesIO, StringIO
from gtts import gTTS
import tempfile
import os
import json
import hashlib

app = Flask(__name__)
app.secret_key = 'some_secret_key'  
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

audio_cache = {}
audio_generation_queue = queue.Queue()
stop_generation_event = threading.Event()
MAX_PRELOADED_FUTURE = 50  
MAX_RETAINED_PAST = 20     

def extract_text_from_pdf(file):
    """Extract text from a PDF file."""
    reader = PyPDF2.PdfReader(file)
    text = ''
    for page in reader.pages:
        text += page.extract_text()
    return text

def extract_text_from_epub(file):
    """Extract text from an EPUB file."""
    book = epub.read_epub(file)
    text = ''
    for item in book.get_items():
        if item.get_type() == epub.EpubHtml:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            text += soup.get_text()
    return text

def extract_text_from_txt(file):
    """Extract text from a plain text file."""
    content = file.read()
    # Handle different encodings
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return content.decode('latin-1')
            except UnicodeDecodeError:
                return content.decode('utf-8', errors='replace')
    return content

def split_into_phrases(text):
    """Split text into phrases based on commas and dots followed by whitespace."""
    phrases = re.split(r'(?<=[.?!])\s+', text)
    return [phrase.strip() for phrase in phrases if phrase.strip()]

def clean_file_paths(text):
    """Remove 'file:///' and everything until '.htm' from the text."""
    pattern = r'file:///.*?\.htm'
    return re.sub(pattern, '', text)

def make_words_clickable(phrase):
    """Convert each word in a phrase to a clickable link for Google search."""
    def replace_word(match):
        word = match.group(0)
        if len(word) > 2 and word.isalpha():  # Only alphabetic words longer than 2 chars
            search_url = f"https://www.google.com/search?q=define+{word}"
            return f'<span class="clickable-word" onclick="window.open(\'{search_url}\', \'_blank\')">{word}</span>'
        return word

    # Split on whitespace and preserve delimiters
    parts = re.split(r'(\s+|[^\w\s]+)', phrase)
    result = ''
    for part in parts:
        if part.strip() and part.isalpha():
            # Process alphabetic words
            result += re.sub(r'\b[a-zA-Z]+\b', replace_word, part)
        else:
            # Keep non-word parts (punctuation, spaces) unchanged
            result += part

    return result

def generate_audio(phrase):
    """Generate audio for a given phrase using gTTS."""
    try:
        # Clean the phrase
        cleaned_phrase = clean_file_paths(phrase)
        
        # Create a buffer to store audio data
        audio_buffer = BytesIO()
        
        # Generate the audio
        tts = gTTS(text=cleaned_phrase, lang='en', slow=False)
        
        # Save the audio to a temporary file, then read it back
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp_file:
            tts.save(tmp_file.name)
            tmp_file.close()
            
            # Read the audio file into the buffer
            with open(tmp_file.name, 'rb') as f:
                audio_buffer.write(f.read())
            
            # Clean up the temporary file
            os.unlink(tmp_file.name)
        
        # Reset the buffer's position
        audio_buffer.seek(0)
        return audio_buffer
    except Exception as e:
        raise Exception(f"Failed to generate audio: {str(e)}")

def audio_preloader_worker():
    """Worker thread that preloads audio in background."""
    while not stop_generation_event.is_set():
        try:
            # Get a phrase from the queue
            index, phrase = audio_generation_queue.get(timeout=1)
            
            # Skip if already cached
            if index in audio_cache:
                audio_generation_queue.task_done()
                continue
                
            # Generate and cache the audio
            audio_buffer = generate_audio(phrase)
            audio_cache[index] = audio_buffer
            
            # Mark the task as done
            audio_generation_queue.task_done()
            
            # Small pause to prevent overloading the system
            time.sleep(0.1)
            
        except queue.Empty:
            # Queue is empty, just continue
            continue
        except Exception as e:
            # Log any errors
            print(f"Error in preloader worker: {str(e)}")
            if 'index' in locals():
                audio_generation_queue.task_done()

def manage_audio_cache(current_index, phrases):
    """Manage the audio cache - keeping past items and scheduling future ones."""
    
    past_start = max(0, current_index - MAX_RETAINED_PAST)
    past_end = current_index
    future_start = current_index + 1
    future_end = min(current_index + MAX_PRELOADED_FUTURE, len(phrases) - 1)
    
    # Determine valid indices to keep in cache
    valid_indices = set(range(past_start, future_end + 1))
    
    # Clean up old cached audio
    keys_to_remove = [k for k in audio_cache.keys() if k not in valid_indices]
    for k in keys_to_remove:
        del audio_cache[k]
    
    # Schedule future phrases for preloading
    for i in range(future_start, future_end + 1):
        if i not in audio_cache:  
            phrase = phrases[i].replace("\n", " ").replace("  ", " ")
            audio_generation_queue.put((i, phrase))

def get_audio_for_phrase(index, phrases):
    """Helper function to get audio for a specific phrase."""
    
    if index in audio_cache:
        # Use cached audio if available
        audio_data = audio_cache[index].getvalue()
        audio_buffer = BytesIO(audio_data)
    else:
        # Generate audio if not cached
        phrase = phrases[index].replace("\n", " ").replace("  ", " ")
        try:
            audio_buffer = generate_audio(phrase)
            # Cache the audio for future use
            audio_cache[index] = BytesIO(audio_buffer.getvalue())
        except Exception as e:
            raise Exception(f"Failed to generate audio: {str(e)}")
    
    return audio_buffer

@app.route('/')
def index():
    """Serve the main HTML page with improved UI focusing on the text."""
    return '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Immersive Document Reader</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root {
                --bg-color: #121212;
                --surface-color: #1e1e1e;
                --surface-lighter: #2d2d2d;
                --primary-color: #bb86fc;
                --secondary-color: #03dac6;
                --text-color: #e0e0e0;
                --muted-color: #9e9e9e;
                --error-color: #cf6679;
                --shadow-color: rgba(0, 0, 0, 0.5);
            }
            
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
                transition: all 0.3s ease;
            }
            
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background-image: url('/static/image.png'); /* Adjust the path and filename as needed */
                background-size: cover; /* Ensures the image covers the entire area */
                background-position: center; /* Centers the image */
                background-repeat: no-repeat; /* Prevents tiling */
                background-attachment: fixed; /* Keeps the background fixed during scroll */
                color: var(--text-color);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                overflow-x: hidden;
            }
            
            header {
                backdrop-filter: blur(20px);
                -webkit-backdrop-filter: blur(20px);
                padding: 1rem;
                box-shadow: 0 2px 10px var(--shadow-color);
                z-index: 10;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            
            .app-title {
                font-size: 1.5rem;
                font-weight: 300;
                letter-spacing: 1px;
                margin: 0;
                color: var(--primary-color);
            }
            
            .controls-toggle {
                background-color: transparent;
                border: none;
                color: var(--text-color);
                font-size: 1.2rem;
                cursor: pointer;
                padding: 0.5rem;
                border-radius: 50%;
            }
            
            .controls-toggle:hover {
                background-color: var(--surface-lighter);
            }
            
            main {
                flex: 1;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 2rem;
                position: relative;
            }
            
            .text-display {
                width: 100%;
                max-width: 800px;
                min-height: 50vh;
                display: flex;
                align-items: center;
                justify-content: center;
                text-align: center;
                font-size: 2rem;
                line-height: 1.5;
                font-weight: 300;
                padding: 2rem;
                position: relative;
                z-index: 1;
            }
            
            .text-content {
                opacity: 1;
                transform: translateY(0);
            }
            
            .fade {
                opacity: 0;
                transform: translateY(20px);
            }
            
            .clickable-word {
                color: var(--text-color);
                cursor: pointer;
                position: relative;
                display: inline-block;
            }
            
            .clickable-word:hover {
                text-decoration: underline;
                background-color: rgba(187, 134, 252, 0.1);
                border-radius: 3px;
            }
            
            .clickable-word:after {
                content: '🔍';
                font-size: 0.7em;
                position: absolute;
                top: -0.7em;
                right: -0.5em;
                opacity: 0;
                transition: opacity 0.2s ease;
            }
            
            .clickable-word:hover:after {
                opacity: 1;
            }
            
            .controls-panel {
                position: fixed;
                top: 0;
                right: -350px;
                height: 100vh;
                width: 350px;
                background-color: var(--surface-color);
                box-shadow: -5px 0 15px var(--shadow-color);
                padding: 1.5rem;
                overflow-y: auto;
                z-index: 100;
                display: flex;
                flex-direction: column;
            }
            
            .controls-panel.visible {
                right: 0;
            }
            
            .controls-section {
                margin-bottom: 2rem;
            }
            
            .section-title {
                font-size: 1rem;
                font-weight: 500;
                margin-bottom: 1rem;
                color: var(--primary-color);
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            
            input[type="file"] {
                display: none;
            }
            
            .file-upload-label {
                display: block;
                background-color: var(--primary-color);
                color: var(--bg-color);
                text-align: center;
                padding: 0.8rem;
                border-radius: 4px;
                cursor: pointer;
                font-weight: 500;
                margin-bottom: 1rem;
            }
            
            .file-upload-label:hover {
                background-color: #9965dd;
            }
            
            .upload-btn {
                width: 100%;
                background-color: var(--secondary-color);
                color: var(--bg-color);
                border: none;
                padding: 0.8rem;
                border-radius: 4px;
                cursor: pointer;
                font-weight: 500;
            }
            
            .upload-btn:disabled {
                background-color: var(--surface-lighter);
                color: var(--muted-color);
                cursor: not-allowed;
            }
            
            .upload-btn:hover:not(:disabled) {
                background-color: #02c4b0;
            }
            
            .search-container {
                display: flex;
                margin-bottom: 1rem;
            }
            
            .search-input {
                flex: 1;
                background-color: var(--surface-lighter);
                border: 1px solid var(--muted-color);
                color: var(--text-color);
                padding: 0.8rem;
                border-radius: 4px 0 0 4px;
            }
            
            .search-btn {
                background-color: var(--primary-color);
                color: var(--bg-color);
                border: none;
                padding: 0.8rem 1rem;
                border-radius: 0 4px 4px 0;
                cursor: pointer;
            }
            
            .search-btn:hover {
                background-color: #9965dd;
            }
            
            .document-info {
                display: flex;
                align-items: center;
                justify-content: space-between;
                background-color: var(--surface-lighter);
                padding: 0.8rem;
                border-radius: 4px;
                margin-bottom: 1rem;
            }
            
            .document-title {
                font-weight: 500;
                word-break: break-all;
            }
            
            .unload-btn {
                background-color: var(--error-color);
                color: var(--bg-color);
                border: none;
                width: 30px;
                height: 30px;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                margin-left: 0.5rem;
            }
            
            .unload-btn:hover {
                background-color: #b5596a;
            }
            
            .navigation-controls {
                display: flex;
                justify-content: center;
                gap: 1rem;
                margin-top: 2rem;
            }
            
            .nav-btn {
                background-color: var(--surface-lighter);
                color: var(--text-color);
                border: none;
                width: 50px;
                height: 50px;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                font-size: 1.2rem;
            }
            
            .nav-btn:hover {
                background-color: var(--primary-color);
                color: var(--bg-color);
            }
            
            .spinner-container {
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 1rem;
            }
            
            .spinner {
                border: 3px solid var(--surface-lighter);
                border-top: 3px solid var(--primary-color);
                border-radius: 50%;
                width: 30px;
                height: 30px;
                animation: spin 1s linear infinite;
                margin-right: 1rem;
            }
            
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            
            .preload-status {
                background-color: var(--surface-lighter);
                border-radius: 4px;
                padding: 1rem;
            }
            
            .preload-container {
                display: flex;
                flex-wrap: wrap;
                justify-content: center;
                margin-top: 0.5rem;
            }
            
            .preload-section {
                margin: 0 10px;
            }
            
            .preload-section-title {
                font-size: 0.75rem;
                margin-bottom: 5px;
                color: var(--muted-color);
            }
            
            .preload-indicator {
                display: inline-block;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                margin: 2px;
                background-color: var(--surface-color);
            }
            
            .preload-loaded {
                background-color: var(--secondary-color);
            }
            
            .preload-current {
                background-color: var(--primary-color);
            }
            
            .preload-past {
                background-color: #03a9f4;
            }
            
            .preload-stats {
                font-size: 0.75rem;
                margin-top: 0.5rem;
                color: var(--muted-color);
                text-align: center;
            }
            
            .overlay {
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background-color: rgba(0, 0, 0, 0.5);
                z-index: 50;
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.3s ease;
            }
            
            .overlay.visible {
                opacity: 1;
                pointer-events: auto;
            }
            
            .hidden {
                display: none;
            }
            
            /* Progress bar */
            .progress-container {
                position: fixed;
                bottom: 0;
                left: 0;
                width: 100%;
                height: 5px;
                backdrop-filter: blur(20px);
                z-index: 5;
            }
            
            .progress-bar {
                height: 100%;
                background-color: var(--primary-color);
                width: 0%;
                transition: width 0.3s ease;
            }
            
            .keyboard-shortcuts {
                padding: 1rem;
                background-color: var(--surface-lighter);
                border-radius: 4px;
                margin-top: 1rem;
            }
            
            .shortcuts-title {
                font-size: 0.9rem;
                color: var(--primary-color);
                margin-bottom: 0.5rem;
            }
            
            .shortcut-item {
                display: flex;
                justify-content: space-between;
                margin: 0.5rem 0;
            }
            
            .key {
                background-color: var(--surface-color);
                padding: 0.2rem 0.5rem;
                border-radius: 3px;
                font-family: monospace;
            }
            
            .reading-history {
                margin-top: 1rem;
                padding: 1rem;
                background-color: var(--surface-lighter);
                border-radius: 4px;
            }
            
            .history-title {
                font-size: 0.9rem;
                color: var(--primary-color);
                margin-bottom: 0.5rem;
            }
            
            .audio-control-btn {
                width: 50px;
                height: 50px;
                border-radius: 50%;
                background-color: var(--primary-color);
                color: var(--bg-color);
                border: none;
                font-size: 1.2rem;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                margin: 0 0.5rem;
            }
            
            .audio-control-btn:hover {
                background-color: #9965dd;
            }
            
            /* Fade in animation for the main content */
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(20px); }
                to { opacity: 1; transform: translateY(0); }
            }
            
            .fade-in {
                animation: fadeIn 0.5s ease forwards;
            }
            
            /* Responsive design */
            @media (max-width: 768px) {
                .controls-panel {
                    width: 280px;
                }
                
                .text-display {
                    font-size: 1.5rem;
                    padding: 1rem;
                }
            }

            .media-grid {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 10px;
            }

            .media-item {
                position: relative;
                width: 100%;
                height: 100px;
                overflow: hidden;
                cursor: pointer;
            }

            .media-item img, .media-item video {
                width: 100%;
                height: 100%;
                object-fit: cover;
            }

            .media-item.selected {
                border: 2px solid var(--primary-color);
            }

            .video-icon {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                color: white;
                font-size: 24px;
                pointer-events: none;
            }
            #background-video {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                object-fit: cover;
                z-index: -1;
            }
            body {
                margin: 0;
                background-color: #000; /* Fallback in case video fails */
            }

            .reading-mode-controls {
                display: flex;
                flex-direction: column;
                gap: 1rem;
                padding: 1rem;
                background-color: var(--surface-lighter);
                border-radius: 4px;
            }

            .toggle-switch {
                position: relative;
                display: inline-flex;
                align-items: center;
                cursor: pointer;
            }

            .toggle-switch input {
                opacity: 0;
                width: 0;
                height: 0;
            }

            .toggle-slider {
                position: relative;
                display: inline-block;
                width: 50px;
                height: 24px;
                background-color: var(--surface-color);
                border-radius: 12px;
                margin-right: 10px;
                transition: 0.3s;
            }

            .toggle-slider:before {
                position: absolute;
                content: "";
                height: 20px;
                width: 20px;
                left: 2px;
                bottom: 2px;
                background-color: var(--text-color);
                border-radius: 50%;
                transition: 0.3s;
            }

            .toggle-switch input:checked + .toggle-slider {
                background-color: var(--primary-color);
            }

            .toggle-switch input:checked + .toggle-slider:before {
                transform: translateX(26px);
            }

            .toggle-label {
                color: var(--text-color);
                font-size: 0.9rem;
            }

            .control-btn {
                background-color: var(--primary-color);
                color: var(--bg-color);
                border: none;
                padding: 0.8rem;
                border-radius: 4px;
                cursor: pointer;
                font-weight: 500;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 0.5rem;
                transition: background-color 0.3s ease;
            }

            .control-btn:hover {
                background-color: #9965dd;
            }
        </style>
    </head>
    <body>
        <video id="background-video" autoplay muted>
            <source src="your-video.mp4" type="video/mp4">
            Your browser does not support the video tag.
        </video>
        <header>
            <h1 class="app-title">Immersive Reader</h1>
            <button id="controlsToggle" class="controls-toggle">
                <i class="fas fa-cog"></i>
            </button>
        </header>
        
        <main>
            <div class="text-display" id="textDisplay">
                <div class="text-content" id="currentPhrase">
                    Upload a document and search for text to begin reading.
                </div>
            </div>
            
            <div class="navigation-controls hidden" id="navigationControls">
                <button class="nav-btn" onclick="prevPhrase()" title="Previous (Left Arrow)">
                    <i class="fas fa-chevron-left"></i>
                </button>
                <button class="nav-btn" onclick="togglePlayPause()" title="Play/Pause (Space)" id="playPauseBtn">
                    <i class="fas fa-pause"></i>
                </button>
                <button class="nav-btn" onclick="replayPhrase()" title="Replay (Up Arrow)">
                    <i class="fas fa-redo"></i>
                </button>
                <button class="nav-btn" onclick="nextPhrase()" title="Next (Right Arrow)">
                    <i class="fas fa-chevron-right"></i>
                </button>
            </div>
        </main>
        
        <div class="progress-container">
            <div class="progress-bar" id="progressBar"></div>
        </div>
        
        <div class="controls-panel" id="controlsPanel">
            <div class="controls-section">
                <h2 class="section-title">Document</h2>
                <div id="uploadSection">
                    <label for="fileInput" class="file-upload-label">
                        <i class="fas fa-file-upload"></i> Choose File
                    </label>
                    <input type="file" id="fileInput" accept=".pdf,.epub,.txt">
                    <button id="uploadBtn" class="upload-btn" onclick="uploadFile()">Upload</button>
                </div>
                
                <div id="documentInfo" class="document-info hidden">
                    <span id="title" class="document-title"></span>
                    <button class="unload-btn" onclick="unloadFile()">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                
                <div id="spinner" class="spinner-container hidden">
                    <div class="spinner"></div>
                    <span>Processing...</span>
                </div>
            </div>
            
            <div id="searchSection" class="controls-section hidden">
                <h2 class="section-title">Navigation</h2>
                <div class="search-container">
                    <input type="text" id="searchInput" class="search-input" placeholder="Search for text...">
                    <button class="search-btn" onclick="search()">
                        <i class="fas fa-search"></i>
                    </button>
                </div>
            </div>
            
            <div id="audioSpinner" class="spinner-container hidden">
                <div class="spinner"></div>
                <span>Generating audio...</span>
            </div>
            
            <div id="preloadStatus" class="preload-status hidden">
                <div>Audio Cache Status:</div>
                <div class="preload-container">
                    <div class="preload-section" id="pastSection">
                        <div class="preload-section-title">Previous</div>
                        <div id="pastIndicators"></div>
                    </div>
                    <div class="preload-section">
                        <div class="preload-section-title">Current</div>
                        <div id="currentIndicator"></div>
                    </div>
                    <div class="preload-section" id="futureSection">
                        <div class="preload-section-title">Next</div>
                        <div id="futureIndicators"></div>
                    </div>
                </div>
                <div class="preload-stats" id="cacheStats"></div>
            </div>
            
            <div class="controls-section">
                <h2 class="section-title">Reading Mode</h2>
                <div class="reading-mode-controls">
                    <label class="toggle-switch">
                        <input type="checkbox" id="silentModeToggle">
                        <span class="toggle-slider"></span>
                        <span class="toggle-label">Silent Reading Mode</span>
                    </label>
                    <button id="startFromBeginning" class="control-btn">
                        <i class="fas fa-redo"></i> Start from Beginning
                    </button>
                </div>
            </div>
            
            <div class="controls-section">
                <h2 class="section-title">Background Media</h2>
                <div id="mediaGrid" class="media-grid"></div>
            </div>
        </div>
        
        <div class="overlay" id="overlay"></div>
        <script>
            let currentAudio = null;
            let preloadedStatus = {};
            let controlsVisible = false;
            let isPlaying = false;
            let currentFilePath = null;
            let currentMedia = { type: 'image', file: 'image.png' }; // Default background
            let isSilentMode = false;
            
            // Toggle controls panel
            function toggleControls() {
                const controlsPanel = document.getElementById('controlsPanel');
                const overlay = document.getElementById('overlay');
                
                controlsVisible = !controlsVisible;
                
                if (controlsVisible) {
                    controlsPanel.classList.add('visible');
                    overlay.classList.add('visible');
                } else {
                    controlsPanel.classList.remove('visible');
                    overlay.classList.remove('visible');
                }
            }
            
            // Handle text transitions with clickable words
            function updateText(text) {
                const textContent = document.getElementById('currentPhrase');
                const textDisplay = document.getElementById('textDisplay');

                // Apply fade out
                textContent.classList.add('fade');

                // After fade out, update text and fade in
                setTimeout(() => {
                    textContent.innerHTML = text;
                    textContent.classList.remove('fade');
                }, 300);
            }
            
            // Update progress bar
            function updateProgressBar(currentIndex, totalPhrases) {
                const progressBar = document.getElementById('progressBar');
                const percentage = (currentIndex / (totalPhrases - 1)) * 100;
                progressBar.style.width = `${percentage}%`;
            }
            
            // Play audio function
            async function playAudio(url) {
                if (currentAudio) {
                    currentAudio.pause();
                    currentAudio.currentTime = 0;
                }
                
                currentAudio = new Audio(url);
                
                currentAudio.addEventListener('playing', () => {
                    document.getElementById('audioSpinner').classList.add('hidden');
                    document.getElementById('playPauseBtn').innerHTML = '<i class="fas fa-pause"></i>';
                    isPlaying = true;
                });
                
                currentAudio.addEventListener('ended', () => {
                    document.getElementById('playPauseBtn').innerHTML = '<i class="fas fa-play"></i>';
                    isPlaying = false;
                });
                
                currentAudio.addEventListener('error', () => {
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert('Error playing audio');
                    isPlaying = false;
                });
                
                try {
                    await currentAudio.play();
                } catch (err) {
                    console.error('Audio playback error:', err);
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert('Error playing audio. Try again or reload the page.');
                    isPlaying = false;
                }
            }
            
            // Toggle play/pause
            function togglePlayPause() {
                if (!currentAudio) return;
                
                if (isPlaying) {
                    currentAudio.pause();
                    document.getElementById('playPauseBtn').innerHTML = '<i class="fas fa-play"></i>';
                    isPlaying = false;
                } else {
                    currentAudio.play();
                    document.getElementById('playPauseBtn').innerHTML = '<i class="fas fa-pause"></i>';
                    isPlaying = true;
                }
            }
            
            async function uploadFile() {
                const fileInput = document.getElementById('fileInput');
                const file = fileInput.files[0];
                
                if (!file) {
                    alert('Please select a file');
                    return;
                }
                
                const uploadButton = document.getElementById('uploadBtn');
                uploadButton.disabled = true;
                
                const spinner = document.getElementById('spinner');
                spinner.classList.remove('hidden');
                
                const formData = new FormData();
                formData.append('file', file);
                
                try {
                    const response = await fetch('/upload', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const result = await response.json();
                    
                    if (result.title) {
                        document.getElementById('title').textContent = result.title;
                        document.getElementById('documentInfo').classList.remove('hidden');
                        document.getElementById('searchSection').classList.remove('hidden');
                        document.getElementById('navigationControls').classList.remove('hidden');
                        
                        // Get the current phrase and audio
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                        }
                        
                        if (!isSilentMode) {
                            const audioResponse = await fetch('/get_current_audio');
                            if (audioResponse.ok) {
                                const blob = await audioResponse.blob();
                                const url = URL.createObjectURL(blob);
                                playAudio(url);
                            }
                        }
                        
                        updatePreloadStatus();
                    } else {
                        alert('Error: ' + result.error);
                    }
                } catch (error) {
                    alert('Upload failed: ' + error.message);
                } finally {
                    spinner.classList.add('hidden');
                    uploadButton.disabled = false;
                }
            }
            
            function unloadFile() {
                fetch('/unload', { method: 'POST' });
                document.getElementById('documentInfo').classList.add('hidden');
                document.getElementById('searchSection').classList.add('hidden');
                document.getElementById('navigationControls').classList.add('hidden');
                document.getElementById('audioSpinner').classList.add('hidden');
                document.getElementById('preloadStatus').classList.add('hidden');
                document.getElementById('fileInput').value = '';
                document.getElementById('progressBar').style.width = '0%';
                updateText('Upload a document and search for text to begin reading.');
                preloadedStatus = {};
                currentFilePath = null;
                if (currentAudio) {
                    currentAudio.pause();
                    currentAudio = null;
                }
                isPlaying = false;
            }
            
            async function updatePreloadStatus() {
                try {
                    const response = await fetch('/preload_status');
                    const status = await response.json();
                    
                    if (Object.keys(status).length > 0) {
                        // Show the preload status section
                        const preloadStatusEl = document.getElementById('preloadStatus');
                        const pastIndicatorsEl = document.getElementById('pastIndicators');
                        const currentIndicatorEl = document.getElementById('currentIndicator');
                        const futureIndicatorsEl = document.getElementById('futureIndicators');
                        const statsEl = document.getElementById('cacheStats');
                        
                        preloadStatusEl.classList.remove('hidden');
                        
                        // Clear previous indicators
                        pastIndicatorsEl.innerHTML = '';
                        currentIndicatorEl.innerHTML = '';
                        futureIndicatorsEl.innerHTML = '';
                        
                        const currentIndex = status.current_index;
                        const cachedIndices = status.cached || [];
                        const totalPhrases = status.total_phrases || 0;
                        
                        // Update progress bar
                        updateProgressBar(currentIndex, totalPhrases);
                        
                        // Create past indicators (up to 20)
                        const pastStart = Math.max(0, currentIndex - 10);
                        for (let i = pastStart; i < currentIndex; i++) {
                            const dot = document.createElement('div');
                            dot.className = 'preload-indicator';
                            if (cachedIndices.includes(i)) {
                                dot.classList.add('preload-past');
                                dot.title = `Cached: ${i}`;
                            } else {
                                dot.title = `Not cached: ${i}`;
                            }
                            pastIndicatorsEl.appendChild(dot);
                        }
                        
                        // Create current indicator
                        const currentDot = document.createElement('div');
                        currentDot.className = 'preload-indicator preload-current';
                        currentDot.title = `Current: ${currentIndex}`;
                        currentIndicatorEl.appendChild(currentDot);
                        
                        // Create future indicators (up to 20 for better display)
                        const futureEnd = Math.min(currentIndex + 20, totalPhrases);
                        for (let i = currentIndex + 1; i <= futureEnd; i++) {
                            const dot = document.createElement('div');
                            dot.className = 'preload-indicator';
                            if (cachedIndices.includes(i)) {
                                dot.classList.add('preload-loaded');
                                dot.title = `Cached: ${i}`;
                            } else {
                                dot.title = `Not cached: ${i}`;
                            }
                            futureIndicatorsEl.appendChild(dot);
                        }
                        
                        // Update stats
                        statsEl.textContent = `Position: ${currentIndex + 1} of ${totalPhrases} | Cached: ${cachedIndices.length} phrases`;
                        
                        preloadedStatus = status;
                    }
                } catch (error) {
                    console.error('Error updating preload status:', error);
                }
            }
            
            async function search() {
                const searchInput = document.getElementById('searchInput');
                const query = searchInput.value.trim();
                
                if (!query) {
                    alert('Please enter a search term');
                    return;
                }
                
                const spinner = document.getElementById('audioSpinner');
                spinner.classList.remove('hidden');
                
                try {
                    const searchResponse = await fetch('/search', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ search_string: query })
                    });
                    
                    const searchResult = await searchResponse.json();
                    
                    if (searchResult.success) {
                        // Get the current phrase text to display
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        
                        if (phraseData.phrase) {
                            document.getElementById('navigationControls').classList.remove('hidden');
                            updateText(phraseData.phrase);
                            
                            // Get and play the audio
                            if (!isSilentMode) {
                                const audioResponse = await fetch('/get_current_audio');
                                if (audioResponse.ok) {
                                    const blob = await audioResponse.blob();
                                    const url = URL.createObjectURL(blob);
                                    playAudio(url);
                                }
                            }
                            
                            updatePreloadStatus();
                        }
                    } else {
                        spinner.classList.add('hidden');
                        alert('Error: ' + (searchResult.error || 'Search failed'));
                    }
                } catch (error) {
                    spinner.classList.add('hidden');
                    alert('Search failed: ' + error.message);
                }
            }
            
            async function nextPhrase() {
                const spinner = document.getElementById('audioSpinner');
                spinner.classList.remove('hidden');
                
                try {
                    const response = await fetch('/next', {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                            if (!isSilentMode) {
                                playAudio(url);
                            }
                            updatePreloadStatus();
                        }
                    } else {
                        const errorData = await response.json();
                        spinner.classList.add('hidden');
                        
                        if (errorData.error === 'End of document') {
                            updateText('You have reached the end of the document.');
                        } else {
                            alert('Error: ' + errorData.error);
                        }
                    }
                } catch (error) {
                    spinner.classList.add('hidden');
                    alert('Error: ' + error.message);
                }
            }
            
            async function prevPhrase() {
                const spinner = document.getElementById('audioSpinner');
                spinner.classList.remove('hidden');
                
                try {
                    const response = await fetch('/prev', {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                            if (!isSilentMode) {
                                playAudio(url);
                            }
                            updatePreloadStatus();
                        }
                    } else {
                        const errorData = await response.json();
                        spinner.classList.add('hidden');
                        
                        if (errorData.error === 'Beginning of document') {
                            updateText('You are at the beginning of the document.');
                        } else {
                            alert('Error: ' + errorData.error);
                        }
                    }
                } catch (error) {
                    spinner.classList.add('hidden');
                    alert('Error: ' + error.message);
                }
            }
            
            async function replayPhrase() {
                const spinner = document.getElementById('audioSpinner');
                spinner.classList.remove('hidden');
                
                try {
                    const response = await fetch('/get_current_audio');
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        
                        if (!isSilentMode) {
                            playAudio(url);
                        }
                        
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                            updatePreloadStatus();
                        }
                    } else {
                        spinner.classList.add('hidden');
                        alert('Error replaying phrase');
                    }
                } catch (error) {
                    spinner.classList.add('hidden');
                    alert('Error: ' + error.message);
                }
            }

            async function startFromBeginning() {
                const spinner = document.getElementById('audioSpinner');
                spinner.classList.remove('hidden');
                
                try {
                    const response = await fetch('/start_from_beginning', {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                            if (!isSilentMode) {
                                playAudio(url);
                            }
                            updatePreloadStatus();
                        }
                    } else {
                        const errorData = await response.json();
                        spinner.classList.add('hidden');
                        alert('Error: ' + errorData.error);
                    }
                } catch (error) {
                    spinner.classList.add('hidden');
                    alert('Error: ' + error.message);
                }
            }

            function setBackground(type, file) {
                const body = document.body;
                const video = document.getElementById('background-video');
                
                // Remove selected class from all items
                document.querySelectorAll('.media-item').forEach(item => item.classList.remove('selected'));
                
                // Add selected class to the clicked item
                const selectedItem = document.querySelector(`.media-item[data-file="${file}"]`);
                if (selectedItem) {
                    selectedItem.classList.add('selected');
                }
                
                currentMedia = { type, file };
                
                if (type === 'image') {
                    body.style.backgroundImage = `url('/static/${file}')`;
                    video.style.display = 'none';
                    video.pause();
                } else {
                    video.src = `/static/${file}`;
                    video.style.display = 'block';
                    video.style.opacity = 1;
                    video.play();
                    body.style.backgroundImage = 'none';
                }
            }
            
            // Set up event listeners
            document.addEventListener('DOMContentLoaded', () => {
                // Toggle controls panel
                document.getElementById('controlsToggle').addEventListener('click', toggleControls);
                document.getElementById('overlay').addEventListener('click', toggleControls);
                
                // Silent mode toggle
                document.getElementById('silentModeToggle').addEventListener('change', (e) => {
                    isSilentMode = e.target.checked;
                    if (isSilentMode && currentAudio) {
                        currentAudio.pause();
                        isPlaying = false;
                        document.getElementById('playPauseBtn').innerHTML = '<i class="fas fa-play"></i>';
                    }
                });
                
                // Start from beginning button
                document.getElementById('startFromBeginning').addEventListener('click', startFromBeginning);
                
                // File input change event
                document.getElementById('fileInput').addEventListener('change', () => {
                    const fileInput = document.getElementById('fileInput');
                    const uploadBtn = document.getElementById('uploadBtn');
                    
                    if (fileInput.files && fileInput.files[0]) {
                        uploadBtn.disabled = false;
                    } else {
                        uploadBtn.disabled = true;
                    }
                });
                
                // Keyboard navigation
                document.addEventListener('keydown', (e) => {
                    // Only respond to keyboard shortcuts if a document is loaded
                    if (document.getElementById('navigationControls').classList.contains('hidden')) {
                        return;
                    }
                    
                    switch (e.key) {
                        case 'ArrowLeft':
                            prevPhrase();
                            break;
                        case 'ArrowRight':
                            nextPhrase();
                            break;
                        case 'ArrowUp':
                            replayPhrase();
                            break;
                        case ' ': // Space bar
                            e.preventDefault(); // Prevent scrolling
                            togglePlayPause();
                            break;
                    }
                });
                
                // Check preload status periodically
                setInterval(updatePreloadStatus, 5000);
                
                // Fetch media files and populate grid
                fetch('/get_media_files')
                    .then(response => response.json())
                    .then(data => {
                        const mediaGrid = document.getElementById('mediaGrid');
                        data.forEach(item => {
                            const gridItem = document.createElement('div');
                            gridItem.className = 'media-item';
                            gridItem.dataset.type = item.type;
                            gridItem.dataset.file = item.file;
                            
                            if (item.type === 'image') {
                                const img = document.createElement('img');
                                img.src = `/static/${item.file}`;
                                gridItem.appendChild(img);
                            } else {
                                const video = document.createElement('video');
                                video.src = `/static/${item.file}`;
                                video.preload = 'metadata';
                                video.muted = true;
                                gridItem.appendChild(video);
                                
                                const icon = document.createElement('div');
                                icon.className = 'video-icon';
                                icon.innerHTML = '<i class="fas fa-play"></i>';
                                gridItem.appendChild(icon);
                            }
                            
                            mediaGrid.appendChild(gridItem);
                            
                            gridItem.addEventListener('click', () => {
                                setBackground(item.type, item.file);
                            });
                        });
                        
                        // Set initial selected item
                        const defaultItem = Array.from(mediaGrid.children).find(item => item.dataset.file === currentMedia.file);
                        if (defaultItem) {
                            defaultItem.classList.add('selected');
                        }
                    });

                // Background video event listeners
                const backgroundVideo = document.getElementById('background-video');
                let isFading = false;

                backgroundVideo.addEventListener('timeupdate', function() {
                    if (!isFading && backgroundVideo.duration && backgroundVideo.currentTime >= backgroundVideo.duration - 0.1) {
                        isFading = true;
                        backgroundVideo.style.transition = 'opacity 0.1s ease';
                        backgroundVideo.style.opacity = 0.7; // Higher opacity to reduce black flash
                        backgroundVideo.currentTime = 0;
                    }
                });

                backgroundVideo.addEventListener('seeked', function() {
                    if (isFading) {
                        backgroundVideo.style.transition = 'opacity 0.1s ease';
                        backgroundVideo.style.opacity = 1; // Reset to full opacity
                        isFading = false;
                    }
                });

                // Fallback in case something goes wrong with timeupdate
                backgroundVideo.addEventListener('ended', function() {
                    backgroundVideo.currentTime = 0;
                    backgroundVideo.style.transition = 'none';
                    backgroundVideo.style.opacity = 1;
                    isFading = false;
                    backgroundVideo.play();
                });
            });
        </script>
    </body>
    </html>
    '''

@app.route('/upload', methods=['POST'])
def upload_file():
    """Handle file upload and text extraction."""
    
    stop_generation_event.set()
    if 'preloader_thread' in app.config and app.config['preloader_thread'].is_alive():
        app.config['preloader_thread'].join(timeout=1)
    
    while not audio_generation_queue.empty():
        try:
            audio_generation_queue.get_nowait()
            audio_generation_queue.task_done()
        except queue.Empty:
            break
    audio_cache.clear()
    
    stop_generation_event.clear()
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
        
    try:
        # Check for valid file types
        if file and (file.filename.endswith('.pdf') or file.filename.endswith('.epub') or file.filename.endswith('.txt')):
            # Create a temporary file to store the uploaded content
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1])
            temp_file.write(file.read())
            temp_file.close()
            
            file.seek(0)  # Reset file pointer after reading
            
            # Extract text based on file type
            if file.filename.endswith('.pdf'):
                text = extract_text_from_pdf(file)
            elif file.filename.endswith('.epub'):
                text = extract_text_from_epub(file)
            elif file.filename.endswith('.txt'):
                text = extract_text_from_txt(file)
                
            phrases = split_into_phrases(text)
            session['phrases'] = phrases
            session['title'] = file.filename
            session['current_index'] = 0
            
            # Start preloading audio
            preloader_thread = threading.Thread(target=audio_preloader_worker, daemon=True)
            preloader_thread.start()
            app.config['preloader_thread'] = preloader_thread
            
            return jsonify({
                'title': file.filename,
                'has_progress': False
            })
        
        return jsonify({'error': 'Invalid file type. Please upload PDF, EPUB, or TXT files.'}), 400
        
    except Exception as e:
        return jsonify({'error': f'Error processing file: {str(e)}'}), 500

@app.route('/search', methods=['POST'])
def search():
    """Search for a string in the document and set the starting position."""
    data = request.get_json()
    search_string = data.get('search_string', '')
    if not search_string:
        return jsonify({'error': 'No search string provided'}), 400
    
    phrases = session.get('phrases', [])
    if not phrases:
        return jsonify({'error': 'No document loaded'}), 400
        
    matching_indices = [i for i, phrase in enumerate(phrases) if search_string.lower() in phrase.lower()]
    if len(matching_indices) == 0:
        return jsonify({'error': 'String not found'})
    else:
        # Set the current index to the first match
        session['current_index'] = matching_indices[0]
        
        # Manage the audio cache for the new position
        manage_audio_cache(matching_indices[0], phrases)
        return jsonify({'success': True})

@app.route('/start_from_beginning', methods=['POST'])
def start_from_beginning():
    """Reset to the beginning of the document."""
    if 'phrases' not in session:
        return jsonify({'error': 'No document loaded'}), 400
    
    # Reset to beginning
    session['current_index'] = 0
    
    try:
        # Get audio for the first phrase
        audio_buffer = get_audio_for_phrase(0, session['phrases'])
        
        # Manage the audio cache
        manage_audio_cache(0, session['phrases'])
        
        return send_file(audio_buffer, mimetype='audio/mp3')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/next', methods=['POST'])
def next_phrase():
    """Move to the next phrase and return its audio."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({'error': 'No document loaded or index not set'}), 400
    
    phrases = session['phrases']
    current_index = session['current_index']
    
    if current_index < len(phrases) - 1:
        # Increment current index
        session['current_index'] += 1
        new_index = session['current_index']
        
        try:
            # Get audio for the new phrase
            audio_buffer = get_audio_for_phrase(new_index, phrases)
            
            # Manage the audio cache
            manage_audio_cache(new_index, phrases)
            
            return send_file(audio_buffer, mimetype='audio/mp3')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'End of document'}), 400

@app.route('/prev', methods=['POST'])
def prev_phrase():
    """Move to the previous phrase and return its audio."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({'error': 'No document loaded or index not set'}), 400
    
    phrases = session['phrases']
    current_index = session['current_index']
    
    if current_index > 0:
        # Decrement current index
        session['current_index'] -= 1
        new_index = session['current_index']
        
        try:
            # Get audio for the new phrase
            audio_buffer = get_audio_for_phrase(new_index, phrases)
            
            # Manage the audio cache
            manage_audio_cache(new_index, phrases)
            
            return send_file(audio_buffer, mimetype='audio/mp3')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Beginning of document'}), 400

@app.route('/get_current_audio', methods=['GET'])
def get_current_audio():
    """Return audio for the current phrase (used after initial search or replay)."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({'error': 'No document loaded or index not set'}), 400
    
    current_index = session['current_index']
    phrases = session['phrases']
    
    try:
        # Get audio for the current phrase
        audio_buffer = get_audio_for_phrase(current_index, phrases)
        
        # Manage the audio cache
        manage_audio_cache(current_index, phrases)
        
        return send_file(audio_buffer, mimetype='audio/mp3')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get_current_phrase', methods=['GET'])
def get_current_phrase():
    """Return the current phrase text for display with clickable words."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({'error': 'No document loaded or index not set'}), 400
    
    phrase = session['phrases'][session['current_index']]
    
    # Clean the phrase and make words clickable
    cleaned_phrase = clean_file_paths(phrase)
    clickable_phrase = make_words_clickable(cleaned_phrase)
    
    return jsonify({'phrase': clickable_phrase})

@app.route('/preload_status', methods=['GET'])
def preload_status():
    """Return the status of cached audio files."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({})
    
    current_index = session['current_index']
    cached_indices = list(audio_cache.keys())
    total_phrases = len(session['phrases'])
    
    return jsonify({
        'current_index': current_index,
        'cached': cached_indices,
        'total_phrases': total_phrases
    })

@app.route('/unload', methods=['POST'])
def unload():
    """Clear the session and stop preloading to allow uploading a new file."""
    
    stop_generation_event.set()
    
    while not audio_generation_queue.empty():
        try:
            audio_generation_queue.get_nowait()
            audio_generation_queue.task_done()
        except queue.Empty:
            break
    audio_cache.clear()
    
    session.clear()
    
    return jsonify({'success': True})

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large. Maximum size is 100MB.'}), 413

@app.route('/get_media_files', methods=['GET'])
def get_media_files():
    """Return a list of image and video files in the static folder."""
    static_folder = os.path.join(app.root_path, 'static')
    files = os.listdir(static_folder)
    media_files = [
        {'type': 'image', 'file': f} if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')) else
        {'type': 'video', 'file': f} for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.mp4'))
    ]
    return jsonify(media_files)

if __name__ == '__main__':
    # Initialize the audio preloader thread
    preloader_thread = threading.Thread(target=audio_preloader_worker, daemon=True)
    preloader_thread.start()
    app.config['preloader_thread'] = preloader_thread
    
    try:
        app.run(debug=True, host='localhost', port=5000)
    finally:
        # Clean up when the application exits
        stop_generation_event.set()
        preloader_thread.join(timeout=2)