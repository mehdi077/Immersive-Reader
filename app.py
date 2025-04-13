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
from io import BytesIO
from gtts import gTTS
import tempfile
import os

app = Flask(__name__)
app.secret_key = 'some_secret_key'  # Required for session management
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# In-memory cache for preloaded audio
audio_cache = {}
audio_generation_queue = queue.Queue()
stop_generation_event = threading.Event()
MAX_PRELOADED_FUTURE = 50  # Maximum number of future preloaded audio files
MAX_RETAINED_PAST = 20     # Maximum number of past audio files to retain

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

def split_into_phrases(text):
    """Split text into phrases based on commas and dots followed by whitespace."""
    phrases = re.split(r'(?<=[.?!])\s+', text)
    return [phrase.strip() for phrase in phrases if phrase.strip()]

def clean_file_paths(text):
    """Remove 'file:///' and everything until '.htm' from the text."""
    pattern = r'file:///.*?\.htm'
    return re.sub(pattern, '', text)

def generate_audio(phrase):
    """Generate audio for a given phrase using gTTS."""
    try:
        # Clean file paths from the phrase
        cleaned_phrase = clean_file_paths(phrase)
        
        # Create a BytesIO object to store the audio
        audio_buffer = BytesIO()
        
        # Generate the audio with gTTS
        tts = gTTS(text=cleaned_phrase, lang='en', slow=False)
        
        # Use a temporary file to save and then read the audio
        # (This avoids some issues with BytesIO and gTTS)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp_file:
            tts.save(tmp_file.name)
            tmp_file.close()
            
            # Read the file into our BytesIO buffer
            with open(tmp_file.name, 'rb') as f:
                audio_buffer.write(f.read())
            
            # Delete the temporary file
            os.unlink(tmp_file.name)
        
        # Reset the buffer position to the beginning
        audio_buffer.seek(0)
        return audio_buffer
    except Exception as e:
        raise Exception(f"Failed to generate audio: {str(e)}")

def audio_preloader_worker():
    """Worker thread that preloads audio in background."""
    while not stop_generation_event.is_set():
        try:
            # Get the next task (index, phrase) with a timeout
            index, phrase = audio_generation_queue.get(timeout=1)
            
            # Skip if we already have this audio cached
            if index in audio_cache:
                audio_generation_queue.task_done()
                continue
                
            # Generate and cache the audio
            audio_buffer = generate_audio(phrase)
            audio_cache[index] = audio_buffer
            
            # Mark task as done
            audio_generation_queue.task_done()
            
            # Small delay to prevent overwhelming the API
            time.sleep(0.1)
            
        except queue.Empty:
            # Timeout on queue.get, just continue the loop
            continue
        except Exception as e:
            # Log the error but continue processing
            print(f"Error in preloader worker: {str(e)}")
            if 'index' in locals():
                audio_generation_queue.task_done()

def manage_audio_cache(current_index, phrases):
    """Manage the audio cache - keeping past items and scheduling future ones."""
    # Define the range of indices to keep (past) and to preload (future)
    past_start = max(0, current_index - MAX_RETAINED_PAST)
    past_end = current_index
    future_start = current_index + 1
    future_end = min(current_index + MAX_PRELOADED_FUTURE, len(phrases) - 1)
    
    # Determine range of valid indices to keep in cache
    valid_indices = set(range(past_start, future_end + 1))
    
    # Remove indices that are outside our range
    keys_to_remove = [k for k in audio_cache.keys() if k not in valid_indices]
    for k in keys_to_remove:
        del audio_cache[k]
    
    # Add future phrases to the generation queue
    for i in range(future_start, future_end + 1):
        if i not in audio_cache:  # Only queue if not already cached
            phrase = phrases[i].replace("\n", "").replace("  ", "")
            audio_generation_queue.put((i, phrase))

def get_audio_for_phrase(index, phrases):
    """Helper function to get audio for a specific phrase."""
    # Check if we have cached audio
    if index in audio_cache:
        # Create a new BytesIO object with a copy of the data to prevent buffer issues
        audio_data = audio_cache[index].getvalue()
        audio_buffer = BytesIO(audio_data)
    else:
        # Generate audio on demand
        phrase = phrases[index].replace("\n", "").replace("  ", "")
        try:
            audio_buffer = generate_audio(phrase)
            # Save to cache (as a new BytesIO to avoid buffer position issues)
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
                background-color: var(--bg-color);
                color: var(--text-color);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                overflow-x: hidden;
            }
            
            header {
                background-color: var(--surface-color);
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
                background-color: var(--surface-lighter);
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
        </style>
    </head>
    <body>
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
                    <input type="file" id="fileInput" accept=".pdf,.epub">
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
            
            <div class="keyboard-shortcuts">
                <div class="shortcuts-title">Keyboard Shortcuts</div>
                <div class="shortcut-item">
                    <span>Previous</span>
                    <span class="key">←</span>
                </div>
                <div class="shortcut-item">
                    <span>Next</span>
                    <span class="key">→</span>
                </div>
                <div class="shortcut-item">
                    <span>Replay</span>
                    <span class="key">↑</span>
                </div>
            </div>
        </div>
        
        <div class="overlay" id="overlay"></div>
        <script>
            let currentAudio = null;
            let preloadedStatus = {};
            let controlsVisible = false;

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

            // Handle text transitions
            function updateText(text) {
                const textContent = document.getElementById('currentPhrase');
                const textDisplay = document.getElementById('textDisplay');
                
                // Apply fade out
                textContent.classList.add('fade');
                
                // After fade out, update text and fade in
                setTimeout(() => {
                    textContent.textContent = text;
                    textContent.classList.remove('fade');
                }, 300);
            }

            // Update progress bar
            function updateProgressBar(currentIndex, totalPhrases) {
                const progressBar = document.getElementById('progressBar');
                const percentage = (currentIndex / (totalPhrases - 1)) * 100;
                progressBar.style.width = `${percentage}%`;
            }

            async function playAudio(url) {
                if (currentAudio) {
                    currentAudio.pause();
                    currentAudio.currentTime = 0;
                }
                currentAudio = new Audio(url);
                currentAudio.addEventListener('playing', () => {
                    document.getElementById('audioSpinner').classList.add('hidden');
                });
                currentAudio.addEventListener('error', () => {
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert('Error playing audio');
                });
                try {
                    await currentAudio.play();
                } catch (err) {
                    console.error('Audio playback error:', err);
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert('Error playing audio. Try again or reload the page.');
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
                        updateText('Document loaded successfully! Use the search box to find a starting point.');
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
                if (currentAudio) {
                    currentAudio.pause();
                    currentAudio = null;
                }
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
                                dot.title = `Preloaded: ${i}`;
                            } else {
                                dot.title = `Not loaded: ${i}`;
                            }
                            futureIndicatorsEl.appendChild(dot);
                        }
                        
                        // Update stats
                        const cachedCount = cachedIndices.length;
                        const pastCount = cachedIndices.filter(i => i < currentIndex).length;
                        const futureCount = cachedIndices.filter(i => i > currentIndex).length;
                        statsEl.textContent = `Phrase ${currentIndex+1} of ${totalPhrases} | Cache: ${cachedCount} segments (${pastCount} past, ${futureCount} future)`;
                    }
                } catch (error) {
                    console.error('Failed to update preload status:', error);
                }
            }

            async function search() {
                const searchString = document.getElementById('searchInput').value;
                if (!searchString) {
                    alert('Please enter a search string');
                    return;
                }
                
                document.getElementById('audioSpinner').classList.remove('hidden');
                
                try {
                    const response = await fetch('/search', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ search_string: searchString })
                    });
                    
                    const result = await response.json();
                    
                    if (result.success) {
                        document.getElementById('navigationControls').classList.remove('hidden');
                        
                        // If controls are open, close them to focus on text
                        if (controlsVisible) {
                            toggleControls();
                        }
                        
                        const audioResponse = await fetch('/get_current_audio');
                        if (audioResponse.ok) {
                            const blob = await audioResponse.blob();
                            const url = URL.createObjectURL(blob);
                            playAudio(url);
                            
                            const phraseResponse = await fetch('/get_current_phrase');
                            const phraseData = await phraseResponse.json();
                            if (phraseData.phrase) {
                                updateText(phraseData.phrase);
                            }
                            
                            updatePreloadStatus();
                        } else {
                            throw new Error(await audioResponse.text());
                        }
                    } else {
                        throw new Error(result.error);
                    }
                } catch (error) {
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert(error.message);
                }
            }

            async function nextPhrase() {
                document.getElementById('audioSpinner').classList.remove('hidden');
                try {
                    const response = await fetch('/next', { method: 'POST' });
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        playAudio(url);
                        
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                        }
                        
                        updatePreloadStatus();
                    } else {
                        throw new Error(await response.text());
                    }
                } catch (error) {
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert('Error: ' + error.message);
                }
            }

            async function prevPhrase() {
                document.getElementById('audioSpinner').classList.remove('hidden');
                try {
                    const response = await fetch('/prev', { method: 'POST' });
                    if (response.ok) {
                        const blob = await response.blob();
                        const url = URL.createObjectURL(blob);
                        playAudio(url);
                        
                        const phraseResponse = await fetch('/get_current_phrase');
                        const phraseData = await phraseResponse.json();
                        if (phraseData.phrase) {
                            updateText(phraseData.phrase);
                        }
                        
                        updatePreloadStatus();
                    } else {
                        throw new Error(await response.text());
                    }
                } catch (error) {
                    document.getElementById('audioSpinner').classList.add('hidden');
                    alert('Error: ' + error.message);
                }
            }

            async function replayPhrase() {
                if (currentAudio) {
                    currentAudio.pause();
                    currentAudio.currentTime = 0;
                    currentAudio.play();
                } else {
                    document.getElementById('audioSpinner').classList.remove('hidden');
                    try {
                        const response = await fetch('/get_current_audio');
                        if (response.ok) {
                            const blob = await response.blob();
                            const url = URL.createObjectURL(blob);
                            playAudio(url);
                        } else {
                            throw new Error(await response.text());
                        }
                    } catch (error) {
                        document.getElementById('audioSpinner').classList.add('hidden');
                        alert('Error: ' + error.message);
                    }
                }
            }

            // Event listeners
            document.addEventListener('DOMContentLoaded', () => {
                // Controls toggle
                document.getElementById('controlsToggle').addEventListener('click', toggleControls);
                document.getElementById('overlay').addEventListener('click', toggleControls);
                
                // Keyboard controls
                document.addEventListener('keydown', function(event) {
                    if (event.key === 'ArrowLeft') {
                        prevPhrase();
                    } else if (event.key === 'ArrowRight') {
                        nextPhrase();
                    } else if (event.key === 'ArrowUp') {
                        replayPhrase();
                    } else if (event.key === 'Escape' && controlsVisible) {
                        toggleControls();
                    }
                });
                
                // Search on Enter key
                document.getElementById('searchInput').addEventListener('keydown', (event) => {
                    if (event.key === 'Enter') {
                        search();
                    }
                });
                
                // File input change event to update label
                document.getElementById('fileInput').addEventListener('change', function() {
                    const fileName = this.files[0] ? this.files[0].name : 'Choose File';
                    const label = document.querySelector('.file-upload-label');
                    label.innerHTML = `<i class="fas fa-file-upload"></i> ${fileName.length > 20 ? fileName.substring(0, 17) + '...' : fileName}`;
                });
            });
            
            // Periodically update preload status
            setInterval(updatePreloadStatus, 1500);
        </script>
    </body>
    </html>
'''

@app.route('/upload', methods=['POST'])
def upload_file():
    """Handle file upload and text extraction."""
    # Stop any previous preloading thread
    stop_generation_event.set()
    if 'preloader_thread' in app.config and app.config['preloader_thread'].is_alive():
        app.config['preloader_thread'].join(timeout=1)
    
    # Clear the queue and cache
    while not audio_generation_queue.empty():
        try:
            audio_generation_queue.get_nowait()
            audio_generation_queue.task_done()
        except queue.Empty:
            break
    audio_cache.clear()
    
    # Reset the stop event
    stop_generation_event.clear()
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if file and (file.filename.endswith('.pdf') or file.filename.endswith('.epub')):
        if file.filename.endswith('.pdf'):
            text = extract_text_from_pdf(file)
        else:
            text = extract_text_from_epub(file)
        phrases = split_into_phrases(text)
        session['phrases'] = phrases
        session['title'] = file.filename
        session['current_index'] = 0  # Set initial index
        
        # Start the preloader thread
        preloader_thread = threading.Thread(target=audio_preloader_worker, daemon=True)
        preloader_thread.start()
        app.config['preloader_thread'] = preloader_thread
        
        return jsonify({'title': file.filename})
    return jsonify({'error': 'Invalid file type'}), 400

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
        # Use the first match (could be enhanced to handle multiple matches better)
        session['current_index'] = matching_indices[0]
        # Start managing audio cache
        manage_audio_cache(matching_indices[0], phrases)
        return jsonify({'success': True})

@app.route('/next', methods=['POST'])
def next_phrase():
    """Move to the next phrase and return its audio."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({'error': 'No document loaded or index not set'}), 400
    phrases = session['phrases']
    current_index = session['current_index']
    if current_index < len(phrases) - 1:
        # Increment the index
        session['current_index'] += 1
        new_index = session['current_index']
        
        try:
            # Get audio using the helper function
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
        # Decrement the index
        session['current_index'] -= 1
        new_index = session['current_index']
        
        try:
            # Get audio using the helper function
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
        # Get audio using the helper function
        audio_buffer = get_audio_for_phrase(current_index, phrases)
        
        # Manage audio cache
        manage_audio_cache(current_index, phrases)
        
        return send_file(audio_buffer, mimetype='audio/mp3')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get_current_phrase', methods=['GET'])
def get_current_phrase():
    """Return the current phrase text for display."""
    if 'phrases' not in session or 'current_index' not in session:
        return jsonify({'error': 'No document loaded or index not set'}), 400
    phrase = session['phrases'][session['current_index']]
    # Clean phrase when returning for display
    cleaned_phrase = clean_file_paths(phrase)
    return jsonify({'phrase': cleaned_phrase})

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
    # Stop the preloader thread
    stop_generation_event.set()
    
    # Clear the queue and cache
    while not audio_generation_queue.empty():
        try:
            audio_generation_queue.get_nowait()
            audio_generation_queue.task_done()
        except queue.Empty:
            break
    audio_cache.clear()
    
    # Clear the session
    session.clear()
    
    return jsonify({'success': True})

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large. Maximum size is 100MB.'}), 413

if __name__ == '__main__':
    # Start the preloader thread when the app starts
    preloader_thread = threading.Thread(target=audio_preloader_worker, daemon=True)
    preloader_thread.start()
    app.config['preloader_thread'] = preloader_thread
    
    try:
        app.run(debug=True, host='localhost', port=5000)
    finally:
        # Clean shutdown of the thread
        stop_generation_event.set()
        preloader_thread.join(timeout=2)
