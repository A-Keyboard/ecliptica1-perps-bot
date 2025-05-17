#!/usr/bin/env python3

with open('ecliptica_bot.py', 'r') as file:
    lines = file.readlines()

fixed_lines = []
in_start_function = False
start_indentation_fixed = False
in_handle_setup = False
handle_indentation_fixed = False

for line in lines:
    # Check if we're entering the start function
    if 'async def start(' in line:
        in_start_function = True
        fixed_lines.append(line)
        continue
    
    # Fix indentation in start function
    if in_start_function and not start_indentation_fixed:
        if 'await update.message.reply_text(' in line and not line.startswith('        '):
            # This is the misaligned line in the start function
            fixed_lines.append('        ' + line.lstrip())
            start_indentation_fixed = True
            continue
        elif 'logger.info("Start message sent successfully")' in line and not line.startswith('        '):
            # This is another potentially misaligned line
            fixed_lines.append('        ' + line.lstrip())
            continue
        elif 'except Exception as e:' in line:
            # We're past the problematic part
            in_start_function = False
    
    # Check if we're entering the handle_setup function
    if 'async def handle_setup(' in line:
        in_handle_setup = True
        fixed_lines.append(line)
        continue
    
    # Fix indentation in handle_setup function
    if in_handle_setup and not handle_indentation_fixed:
        if ('_, key, value = data' in line or 
            'ctx.user_data["ans"][key] = value' in line or 
            'ctx.user_data["i"] += 1' in line) and not line.startswith('        '):
            # These are the misaligned lines in handle_setup
            fixed_lines.append('        ' + line.lstrip())
            continue
        elif 'await query.answer(' in line and not line.startswith('        '):
            # Another potentially misaligned line
            fixed_lines.append('        ' + line.lstrip())
            continue
        elif 'except Exception as e:' in line:
            # We're past the problematic part
            in_handle_setup = False
            handle_indentation_fixed = True
    
    # Add all other lines unchanged
    fixed_lines.append(line)

with open('ecliptica_bot_fixed.py', 'w') as file:
    file.writelines(fixed_lines)

print("Fixed file written to ecliptica_bot_fixed.py") 