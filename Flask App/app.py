from flask import Flask, request, redirect, url_for, render_template, flash
import requests
import os
import time
from time import sleep
from my_model import check_phishing
from config import VIRUSTOTAL_API_KEY, OPENAI_API_KEY
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from zoneinfo import ZoneInfo
import re
import plotly
import plotly.graph_objs as go
from collections import defaultdict
from openai import OpenAI

client = OpenAI(api_key=OPENAI_API_KEY)
import magic
from email import message_from_bytes, policy
from email.parser import BytesParser
from email.utils import parseaddr
import pdfplumber
import docx
import chardet


app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Needed for flash messages and sessions

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///phishScope.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Blacklist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)

class EmailAnalytics(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(50), nullable=False)  # 'safe' or 'unsafe'
    date_analyzed = db.Column(db.DateTime, default=lambda: datetime.now(ZoneInfo("America/New_York")))
    score = db.Column(db.Float, nullable=True)  # Add this line

with app.app_context():
    db.create_all()

# Define the allowed extensions for upload
ALLOWED_EXTENSIONS = {'eml', 'txt', 'pdf', 'docx'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    # Render the upload form template
    return render_template('upload.html', time=time) 

@app.route('/upload', methods=['POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '' or not allowed_file(file.filename):
            flash('No selected file or file type not allowed')
            return redirect(request.url)

        flash('Upload Successful')

        # Get the file type
        file_type = magic.from_buffer(file.read(), mime=True)

        # Reset the file stream
        file.seek(0)

        if file_type == 'text/plain':
            # Handle plain text files
            content = file.read().decode('utf-8')
            email_address = extract_email_address(content)
            mail_body = content
        elif file_type == 'message/rfc822':
            # Handle email files (e.g., .eml)
            email_message = message_from_bytes(file.read(), policy=policy.default)
            email_address = extract_email_address(email_message['From'])
            mail_body = get_body(email_message)
        elif file_type == 'application/pdf':
            # Handle PDF files
            with pdfplumber.open(file) as pdf:
                mail_body = ''
                for page in pdf.pages:
                    mail_body += page.extract_text()
            email_address = extract_email_address(mail_body)
        elif file_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
            # Handle DOCX files
            doc = docx.Document(file)
            mail_body = ''
            for para in doc.paragraphs:
                mail_body += para.text + '\n'
            email_address = extract_email_address(mail_body)
        else:
            # Handle other file types (e.g., .jpg, .png, etc.)
            flash(f'File type {file_type} is not supported')
            return redirect(request.url)

        # Call the check_phishing function
        phishing_results = check_phishing(mail_body)

        # Initialize variables
        phishing_detected = False

        if phishing_results:
            label = phishing_results[0]['label']
            score = phishing_results[0]['score']

            if label == 'phishing':
                phishing_detected = True

                if email_address:
                    blacklist_email(email_address)
                    flash('Phishing detected. Sender added to blacklist.')
                else:
                    flash('Phishing detected, but no email address found.')

                flash(f"Phishing score: {score}")
            else:
                # Flash message for benign or other non-phishing labels
                flash(f"{label.capitalize()} score: {score}")
                flash('Content deemed safe.')

        else:
            flash('No results from phishing check.')

        # Log the phishing check result
        status = 'unsafe' if phishing_detected else 'safe'
        new_analytics = EmailAnalytics(email=email_address or 'unknown', status=status, score=phishing_results[0]['score'] if phishing_detected else 0)
        db.session.add(new_analytics)
        db.session.commit()

        return redirect(url_for('index'))

    return redirect(url_for('index'))

def get_body(email_message):
    if email_message.is_multipart():
        for payload in email_message.get_payload():
            body = get_body(payload)
            if body:
                return body
    else:
        payload = email_message.get_payload(decode=True)
        if payload:
            # Try to detect the encoding
            result = chardet.detect(payload)
            encoding = result['encoding'] if result['encoding'] else 'utf-8'

            try:
                # Try decoding the payload using the detected encoding
                body = payload.decode(encoding, errors='replace')
                return body
            except (UnicodeDecodeError, LookupError):
                # If the decoding fails, try other encodings
                for encoding in ['windows-1252', 'latin-1']:
                    try:
                        body = payload.decode(encoding, errors='replace')
                        return body
                    except UnicodeDecodeError:
                        pass

    return None

def extract_email_address(content):
    # First, try to parse the content as an email address using the standard library
    parsed_email = parseaddr(content)[1]
    if parsed_email:
        return parsed_email

    # If parsing fails, try the regular expression approach
    match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', content)
    return match.group(0) if match else None

def blacklist_email(email_address):
    # Check if the email address is already in the blacklist to prevent duplicates
    if not Blacklist.query.filter_by(email=email_address).first():
        new_blacklist_entry = Blacklist(email=email_address)
        db.session.add(new_blacklist_entry)
        db.session.commit()


@app.route('/attachment-upload', methods=['POST'])
def attachment_upload():
    # Check if a file is part of the uploaded request
    if 'attachment' not in request.files:
        flash('No file part')
        return redirect(url_for('index'))

    # Retrieve the file from the form
    file = request.files['attachment']
    
    # Check if the filename is not empty and allowed
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('index'))
    
    if file and allowed_file(file.filename):
        # Prepare the request to VirusTotal
        files = {'file': (file.filename, file.stream, 'application/octet-stream')}
        headers = {'x-apikey': VIRUSTOTAL_API_KEY}

        # Send the file to the VirusTotal API for scanning
        response = requests.post('https://www.virustotal.com/api/v3/files', files=files, headers=headers)
        
        # Check if the request was successful
        if response.status_code == 200:
            json_response = response.json()
            resource_id = json_response['data']['id']
        
        # Wait for 15 seconds before attempting to retrieve the report
            sleep(15)

        # Make a GET request to retrieve the file report
            report_url = f"https://www.virustotal.com/api/v3/analyses/{resource_id}"
            report_headers = {"accept": "application/json", "x-apikey": VIRUSTOTAL_API_KEY}
            report_response = requests.get(report_url, headers=report_headers)

        if report_response.status_code == 200:
            report = report_response.json()
            stats = report['data']['attributes']['stats']
    
    # Instead of redirecting, render the 'upload.html' template with the results
            return render_template('upload.html', stats=stats, show_results=True, current_time=time.time())
        else:
            flash('Failed to retrieve the report.')
    else:
        flash('Failed to scan the file. Please try again.')

    return redirect(url_for('index'))

@app.route('/url', methods=['POST'])
def upload_url():
    if request.method == 'POST':
        # Check if the post request has the URL part
        if 'url' not in request.form:
            flash('No URL part')
            return redirect(request.url)
        url = request.form['url']
        # If user does not enter URL
        if url == '':
            flash('No URL entered')
            return redirect(request.url)
        # Here, you can add your processing logic
        flash('URL successfully uploaded')
        return redirect(url_for('index'))
    return redirect(url_for('index'))

@app.route('/analytics')
def analytics():
    # Query the EmailAnalytics table to get the data
    email_analytics = EmailAnalytics.query.all()

    # Count the number of safe and unsafe emails
    safe_count = sum(1 for email in email_analytics if email.status == 'safe')
    unsafe_count = sum(1 for email in email_analytics if email.status == 'unsafe')

    # Create the pie chart data
    labels = ['Safe', 'Unsafe']
    values = [safe_count, unsafe_count]

    # Create the pie chart
    pie_chart = go.Figure(data=[go.Pie(labels=labels, values=values)])
    pie_div = plotly.offline.plot(pie_chart, output_type='div', include_plotlyjs=False)

    # Get the data grouped by date and status
    email_data = defaultdict(lambda: {'safe': 0, 'unsafe': 0})
    for email in email_analytics:
        date = email.date_analyzed.date()
        status = email.status
        email_data[date][status] += 1

    # Create the line chart data
    dates = sorted(email_data.keys())
    safe_counts = [email_data[date]['safe'] for date in dates]
    unsafe_counts = [email_data[date]['unsafe'] for date in dates]

    # Create the line chart
    line_chart = go.Figure()
    line_chart.add_trace(go.Scatter(x=dates, y=safe_counts, mode='lines', name='Safe'))
    line_chart.add_trace(go.Scatter(x=dates, y=unsafe_counts, mode='lines', name='Unsafe'))
    line_chart.update_layout(title='Email Analysis Trend', xaxis_title='Date', yaxis_title='Count')

    # Convert the plotly figure to HTML
    line_div = plotly.offline.plot(line_chart, output_type='div', include_plotlyjs=False)

    # Group the email data by score range and status
    score_ranges = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1)]
    email_data = {(start, end): {'safe': 0, 'unsafe': 0} for start, end in score_ranges}
    for email in email_analytics:
        score = email.score
        status = email.status
        for start, end in score_ranges:
            if start <= score < end:
                email_data[(start, end)][status] += 1

    # Create the bar chart data
    x_labels = [f"{start:.1f} - {end:.1f}" for start, end in score_ranges]
    safe_counts = [email_data[(start, end)]['safe'] for start, end in score_ranges]
    unsafe_counts = [email_data[(start, end)]['unsafe'] for start, end in score_ranges]

    # Create the bar chart
    bar_chart = go.Figure(data=[
        go.Bar(x=x_labels, y=safe_counts, name='Safe'),
        go.Bar(x=x_labels, y=unsafe_counts, name='Unsafe')
    ])
    bar_chart.update_layout(title='Email Status Distribution by Score Range', xaxis_title='Score Range', yaxis_title='Count', barmode='group')

    # Convert the plotly figure to HTML
    bar_div = plotly.offline.plot(bar_chart, output_type='div', include_plotlyjs=False)

    # Get the data for score and analysis date
    scores = [email.score for email in email_analytics]
    dates = [email.date_analyzed for email in email_analytics]

    # Create the scatter plot
    scatter_plot = go.Figure(data=go.Scatter(x=dates, y=scores, mode='markers'))
    scatter_plot.update_layout(title='Email Score vs. Analysis Date', xaxis_title='Analysis Date', yaxis_title='Score')

    # Convert the plotly figure to HTML
    scatter_div = plotly.offline.plot(scatter_plot, output_type='div', include_plotlyjs=False)

    return render_template('analytics.html', pie_div=pie_div, line_div=line_div, bar_div=bar_div, scatter_div=scatter_div)

@app.route('/chatbot', methods=['GET', 'POST'])
def chatbot():
    if request.method == 'POST':
        user_input = request.form['user_input']
        response = generate_response(user_input)
        return render_template('chatbot.html', response=response)
    return render_template('chatbot.html')


def generate_response(prompt):
    response = client.chat.completions.create(model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": prompt}
    ])

    message = response.choices[0].message.content
    return message

@app.route('/blacklist', methods=['GET', 'POST'])
def manage_blacklist():
    if request.method == 'POST':
        # Handle adding or removing emails from the blacklist
        if 'add_email' in request.form:
            email = request.form['email']
            if not Blacklist.query.filter_by(email=email).first():
                new_blacklist_entry = Blacklist(email=email)
                db.session.add(new_blacklist_entry)
                db.session.commit()
                flash(f'Email {email} added to the blacklist.', 'success')
            else:
                flash(f'Email {email} is already in the blacklist.', 'warning')
        elif 'remove_email' in request.form:
            email = request.form['email']
            blacklist_entry = Blacklist.query.filter_by(email=email).first()
            if blacklist_entry:
                db.session.delete(blacklist_entry)
                db.session.commit()
                flash(f'Email {email} removed from the blacklist.', 'success')
            else:
                flash(f'Email {email} is not in the blacklist.', 'warning')

    # Retrieve the blacklist entries from the database
    blacklist_entries = Blacklist.query.all()

    return render_template('blacklist.html', blacklist_entries=blacklist_entries)

if __name__ == '__main__':
    app.run(debug=True, port=9001)
