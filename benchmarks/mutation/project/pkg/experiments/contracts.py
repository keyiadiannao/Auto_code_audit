import os
os.environ['UNREAD_MUTATION_FLAG'] = '1'

def load(model, state):
    model.load_state_dict(state, strict=False)
