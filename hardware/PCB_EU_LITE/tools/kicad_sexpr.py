"""KiCad S-expression helpers extracted from PCB_EU. SPDX-License-Identifier: MIT."""
import json
import re

TOKEN=re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')

def spans(text,key):
    """Balanced forms, ignoring parentheses inside quoted strings."""
    stack=[];result=[];want_key=False
    for t in TOKEN.finditer(text):
        s=t.group()
        if s=='(':
            stack.append([t.start(),None]);want_key=True
        elif s==')':
            start,k=stack.pop()
            if k==key:result.append((start,t.end()))
            want_key=False
        elif want_key:
            stack[-1][1]=s;want_key=False
    if stack:raise ValueError('Unbalanced KiCad data')
    return result

def parse(text):
    stack=[];root=None
    for token in re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text):
        if token=='(':
            item=[]
            if stack:stack[-1].append(item)
            stack.append(item)
        elif token==')':
            root=stack.pop()
        else:
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    if stack or not isinstance(root,list):raise ValueError('Invalid S-expression')
    return root

def children(node, key):
    return [x for x in node if isinstance(x,list) and x and x[0]==key]

def child(node,key):
    found=children(node,key)
    if len(found)!=1:raise ValueError((key,len(found)))
    return found[0]
