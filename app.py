from flask import Flask, request, render_template, send_file, make_response
import os
import pdfplumber
import pandas as pd
import re
import io
from reportlab.lib.pagesizes import letter
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.colors import HexColor

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

latest_df = None  # Global to store last parsed table

def extract_courses_from_pdf(pdf_file):
    data = []
    raw_line_count = 0
    matched_line_count = 0
    student_name = None
    student_no = None

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            # Try to extract NAME and STUDENT NO from the first page only
            if student_name is None or student_no is None:
                for line in text.split('\n'):
                    if student_name is None:
                        match = re.search(r'NAME\s*[:\-]\s*(.*?)(?:\s+STUDENT\s*NO[:\-]|$)', line, re.IGNORECASE)
                        if match:
                            student_name = match.group(1).strip()
                    if student_no is None:
                        match = re.search(r'STUDENT\s*NO\s*[:\-]\s*([A-Za-z0-9\-]+)', line, re.IGNORECASE)
                        if match:
                            student_no = match.group(1).strip()

            for line in text.split('\n'):
                raw_line_count += 1
                line = line.strip()

                # Only process lines that start with a grade
                if not re.match(r'^\d\.\d{2}', line):
                    continue

                tokens = line.split()
                try:
                    grade = float(tokens[0])
                    course_code = tokens[1]
                    idx = 2
                    # If next token is all caps, it's part of the course code (e.g., IT 101)
                    if idx < len(tokens) and re.match(r'^[A-Z0-9]+$', tokens[idx]):
                        course_code += ' ' + tokens[idx]
                        idx += 1
                    # Now, try to find the units (last float/int after course code)
                    units = None
                    units_idx = None
                    for i, token in enumerate(reversed(tokens[idx:])):
                        if re.match(r'^\d+(\.\d+)?$', token):
                            units = float(token)
                            # Find the index of the units in the original tokens list
                            units_idx = len(tokens) - 1 - i
                            print(f"Extracted units: {units} from token: {token} for line: {tokens}")
                            break
                    if units is None:
                        units = 6.0 if course_code == 'IT 402' else 3.0
                        print(f"⚠️ Fallback units used for {course_code}: {units} (tokens: {tokens})")
                        # If fallback, try to get course name from the rest
                        course_name = ' '.join(tokens[idx:])
                    else:
                        # Course Name is everything between units and the next course code or end of line
                        # tokens[idx:units_idx] is the course name, but if units is at the end, this works too
                        course_name = ' '.join(tokens[idx:units_idx])

                    # Exclude PE, NSTP, STEM
                    if course_code.startswith(('PE', 'NSTP', 'STEM')):
                        continue

                    data.append({
                        'Course Code': course_code,
                        'Course Name': course_name.strip(),
                        'Units': units,
                        'Grade': grade
                    })
                    matched_line_count += 1
                except Exception as e:
                    print("❌ Failed to parse line:", line)
                    print("   Error:", e)

    print("📄 Total lines scanned:", raw_line_count)
    print("✅ Parsed courses (non-PE/NSTP/STEM):", matched_line_count)
    print("📊 Final extracted course count:", len(data))
    print("👤 Name:", student_name)
    print("🆔 Student No:", student_no)

    return pd.DataFrame(data), student_name, student_no

@app.route('/', methods=['GET', 'POST'])
def index():
    gwa_result = None
    parsed_courses = []
    total_units = 0
    total_weighted = 0
    student_name = None
    student_no = None
    error_message = None

    if request.method == 'POST':
        file = request.files['file']
        if not file.filename.endswith('.pdf'):
            error_message = "Invalid file type. Please upload a PDF file."
        else:
            try:
                df, student_name, student_no = extract_courses_from_pdf(file.stream)
                if df.empty:
                    error_message = "No valid course data found in the PDF. Please upload a valid transcript."
                else:
                    df_filtered = df[~df['Course Code'].str.startswith(('PE', 'NSTP', 'STEM'))]
                    parsed_courses = df_filtered.to_dict(orient='records')

                    total_units = df_filtered['Units'].sum()
                    total_weighted = (df_filtered['Units'] * df_filtered['Grade']).sum()
                    gwa_result = round(total_weighted / total_units, 4) if total_units > 0 else "No valid data"

                    global latest_df, latest_student_name, latest_student_no
                    latest_df = df_filtered.copy()
                    latest_student_name = student_name
                    latest_student_no = student_no
            except Exception as e:
                error_message = "Failed to process the PDF. Please make sure you uploaded a valid transcript PDF file."

    return render_template('index.html',
                           gwa=gwa_result,
                           courses=parsed_courses,
                           total_units=total_units,
                           total_weighted=total_weighted,
                           student_name=student_name,
                           student_no=student_no,
                           error_message=error_message)

# Store latest student info for PDF download
latest_student_name = None
latest_student_no = None

@app.route('/download')
def download_pdf():
    global latest_df, latest_student_name, latest_student_no
    if latest_df is None or latest_df.empty:
        return "No data available to download.", 400

    df = latest_df.copy()
    #For dummy testing
    #df = pd.concat([
    #    df,
    #    pd.DataFrame([{
    #        'Course Code': 'DUMMY 101',
    #        'Course Name': 'Dummy Course',
    #        'Units': 3,
    #        'Grade': 2.75
    #    }])
    #], ignore_index=True)
    df['Weighted Grade'] = df['Units'] * df['Grade']

    total_units = df['Units'].sum()
    total_weighted = df['Weighted Grade'].sum()
    gwa = round(total_weighted / total_units, 4)

    # --- Latin honor logic (corrected) ---
    latin_honor = "No Latin Honor"

    if 1.00 <= gwa <= 1.25 and (df['Grade'] <= 1.50).all():
        latin_honor = "Summa Cum Laude"
    elif 1.00 <= gwa <= 1.50 and (df['Grade'] <= 2.00).all():
        latin_honor = "Magna Cum Laude"
    elif 1.00 <= gwa <= 1.75 and (df['Grade'] <= 2.50).all():
        latin_honor = "Cum Laude"
    elif 1.00 <= gwa <= 1.75:
        latin_honor = "With Distinction"

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    elements = []

    styles = getSampleStyleSheet()
    title_style = styles['Title']
    title_style.fontName = 'Helvetica-Bold'
    title_style.fontSize = 22
    title_style.textColor = colors.black

    subtitle_style = ParagraphStyle(
        'Subtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        textColor=colors.black,
        spaceAfter=8,
    )

    info_style = ParagraphStyle(
        'Info',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=11,
        textColor=colors.black,
        spaceAfter=4,
    )

    # Modern, luxurious black and white look (use softer black/gray)
    table_header_bg = HexColor("#222326")  # Softer black for header
    table_header_fg = colors.white
    table_row_alt_bg = colors.whitesmoke
    table_row_bg = colors.white
    table_total_bg = colors.lightgrey
    table_gwa_bg = HexColor("#222326")     # Softer black for GWA row
    table_gwa_fg = colors.white

    # Title
    elements.append(Paragraph("GWA Computation", title_style))
    elements.append(Spacer(1, 16))

    # Student info
    if latest_student_name:
        elements.append(Paragraph(f"<b>Name:</b> {latest_student_name}", info_style))
    if latest_student_no:
        elements.append(Paragraph(f"<b>Student No:</b> {latest_student_no}", info_style))
    if latest_student_name or latest_student_no:
        elements.append(Spacer(1, 12))

    # Table data
    table_data = [['#', 'Course Code', 'Course Name', 'Units', 'Grade', 'Weighted Grade']]
    course_name_style = ParagraphStyle('CourseName', alignment=TA_CENTER, fontName='Helvetica', fontSize=10, leading=12, textColor=colors.black)
    for i, row in df.iterrows():
        table_data.append([
            i + 1,
            row['Course Code'],
            Paragraph(str(row['Course Name']), course_name_style),
            row['Units'],
            row['Grade'],
            f"{row['Weighted Grade']:.2f}"
        ])

    # Total row
    table_data.append(['', 'TOTAL', '', f"{total_units}", '', f"{total_weighted:.2f}"])

    # GWA row (merged, modern look, show actual values)
    gwa_formula = (
        f"<b>GWA = Total Weighted ÷ Total Units<br/>"
        f"({total_weighted:.2f} ÷ {total_units})</b>"
    )
    table_data.append([
        Paragraph(gwa_formula, ParagraphStyle('GWA', alignment=TA_CENTER, fontName='Helvetica-Bold', fontSize=11, textColor=table_gwa_fg, leading=14)),
        '', '', '', '', f"{gwa}"
    ])

    table = Table(table_data, colWidths=[30, 70, 225, 50, 50, 85], repeatRows=1)
    table_style = TableStyle([
        # Header
        ('BACKGROUND', (0, 0), (-1, 0), table_header_bg),
        ('TEXTCOLOR', (0, 0), (-1, 0), table_header_fg),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        # Body
        ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -3), 'CENTER'),  # Center align Course Name
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        # Alternating row background
        ('BACKGROUND', (0, 1), (-1, -3), table_row_bg),
        ('ROWBACKGROUNDS', (0, 1), (-1, -3), [table_row_bg, table_row_alt_bg]),
        # Total row
        ('BACKGROUND', (0, -2), (-1, -2), table_total_bg),
        ('FONTNAME', (0, -2), (-1, -2), 'Helvetica-Bold'),
        # GWA row
        ('BACKGROUND', (0, -1), (-1, -1), table_gwa_bg),
        ('TEXTCOLOR', (0, -1), (-1, -1), table_gwa_fg),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, -1), (-1, -1), 12),
        # Merge the first five columns in the last row (GWA row)
        ('SPAN', (0, -1), (4, -1)),
        ('ALIGN', (0, -1), (4, -1), 'CENTER'),
        ('ALIGN', (5, -1), (5, -1), 'CENTER'),
        # Borders
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 10),
        ('TOPPADDING', (0, -1), (-1, -1), 10),
    ])
    table.setStyle(table_style)

    elements.append(table)
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(f"<b>Latin Honor Qualification:</b> {latin_honor}", subtitle_style))

    doc.build(elements)
    buffer.seek(0)

    surname = "Student"
    if latest_student_name:
        if ',' in latest_student_name:
            # Format: "Surname, Firstname Middlename"
            surname = latest_student_name.split(',')[0].strip().title()
        else:
            # Format: "Firstname Middlename Surname"
            surname = latest_student_name.split()[-1].strip().title()

    filename = f"{surname}_GWA Computation.pdf"

    # Secure download headers
    response = make_response(send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype='application/pdf'
    ))
    response.headers['Content-Security-Policy'] = "default-src 'none';"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')