"""Sample questions with a phrase that must appear in the right chunk, for the comparison tools.

Written for the Northwind sample handbook (docs/sample_documents) and the sample resume. Replace with your own.
"""
QUESTIONS = [
    {'q': 'How many days of annual leave do I get?', 'expect': '24 days'},
    {'q': 'What happens to unused leave at the end of the year?', 'expect': 'carried forward'},
    {'q': 'How long is parental leave for the second caregiver?', 'expect': '4 weeks'},
    {'q': 'time off after having a baby', 'expect': 'parental leave'},
    {'q': 'how long to report phishing', 'expect': '1 hour'},
    {'q': 'minimum password length', 'expect': '12 characters'},
    {'q': 'VPN on public wifi', 'expect': 'VPN'},
    {'q': 'USB', 'expect': 'USB'},
    {'q': '30-60-90', 'expect': '30-60-90'},
    {'q': 'expense claims deadline', 'expect': '30 days'},
    {'q': 'Does Mohan know LangGraph?', 'expect': 'LangGraph'},
    {'q': 'FAISS', 'expect': 'FAISS'},
]
