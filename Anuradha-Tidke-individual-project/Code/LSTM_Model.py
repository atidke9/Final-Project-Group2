# %% --------------------------------------- Imports -------------------------------------------------------------------
import os
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import accuracy_score, confusion_matrix
import nltk
from tqdm import tqdm
from sklearn.model_selection import train_test_split

"""Download glove embedding file by uncommenting the following code chunk"""
# if "glove.6B.50d.txt" not in os.listdir(os.getcwd()):
#     try:
#         os.system("wget http://nlp.stanford.edu/data/glove.6B.zip")
#         os.system("unzip glove.6B.zip")
#         os.system("mv glove.6B/glove.6B.50d.txt glove.6B.50d.txt")
#         os.system("sudo rm -r glove.6B")
#     except:
#         print("There as a problem downloading the data!")
#         raise
#     if "glove.6B.50d.txt" not in os.listdir(os.getcwd()):
#         print("There as a problem downloading the data!")
#         # Download glove.6B.zip from https://nlp.stanford.edu/projects/glove/, unzip it and move glove.6B.50d.txt to the
#         # current working directory.

# %% --------------------------------------- Set-Up --------------------------------------------------------------------
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
random_seed = 42
torch.manual_seed(random_seed)
np.random.seed(random_seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
TRAIN, SAVE_MODEL = True, True

# %% ----------------------------------- Hyper Parameters --------------------------------------------------------------
class Args:
    def __init__(self):
        self.seq_len = "get_max_from_data"  # Important Note: LSTMs can process variable length sequences, so we do not
        # need to zero-pad all the sentences to a fixed length value. However, if we want to do vectorized mini-batching
        # we do need to do so. Otherwise, we could still do mini-batch GD by aggregating the gradients but we would need
        # to process each sentence one by one instead of in batches, which would be pretty slow, but maybe worth it!
        self.use_packed_sequence = True  # Another way (and probably better) of processing variable length sequences is by
        # using torch.nn.utils.rnn.PackedSequence. This way we are still padding BUT the padded zeros are not shown to the LSTM.
        self.embedding_dim = 50
        self.n_epochs = 10
        self.lr = 1e-3
        self.batch_size = 32
        self.hidden_size = 16
        self.n_layers = 3
        self.lstm_drop = 0.5
        self.lin_drop = 0.5

args = Args()

# %% ----------------------------------- Helper Functions --------------------------------------------------------------
def acc(x, y, return_labels=False):
    with torch.no_grad():
        logits = torch.empty(len(x), 2)
        for batch in range(len(x) // args.batch_size + 1):
            inds = slice(batch * args.batch_size, (batch + 1) * args.batch_size)
            logits[inds] = model(x[inds])
        pred_labels = np.argmax(logits.cpu().numpy(), axis=1)
    if return_labels:
        return pred_labels
    else:
        return 100*accuracy_score(y.cpu().numpy(), pred_labels)

def extract_vocab_dict_and_msl(sentences_train, sentences_dev, sentences_val):
    """ Tokenizes all the sentences and gets a dictionary of unique tokens and also the maximum sequence length """
    tokens, ms_len = [], 0
    for sentence in list(sentences_train) + list(sentences_dev) + list(sentences_val):
        tokens_in_sentence = nltk.word_tokenize(sentence)
        if ms_len < len(tokens_in_sentence):
            ms_len = len(tokens_in_sentence)
        tokens += tokens_in_sentence
    token_vocab = {key: i for key, i in zip(set(tokens), range(1, len(set(tokens))+1))}
    return token_vocab, ms_len

def convert_to_ids(raw_sentences, vocab_dict, pad_to):
    """ Takes an NumPy array of raw text sentences and converts to a sequence of token ids """
    x = np.empty((len(raw_sentences), pad_to))
    for idx, sentence in enumerate(raw_sentences):
        word_ids = []
        for token in nltk.word_tokenize(sentence):
            try:
                word_ids.append(vocab_dict[token])
            except:
                word_ids.append(vocab_dict[token])
        if pad_to < len(word_ids):
            x[idx] = word_ids[:pad_to]
        else:
            x[idx] = word_ids + [0] * (pad_to - len(word_ids))
    return x

def get_glove_embeddings(vocab_dict):
    with open("glove.6B.50d.txt", "r") as s:
        glove = s.read()
    embeddings_dict = {}
    for line in glove.split("\n")[:-1]:
        text = line.split()
        if text[0] in vocab_dict:
            embeddings_dict[vocab_dict[text[0]]] = torch.from_numpy(np.array(text[1:], dtype="float32"))
    return embeddings_dict

def get_glove_table(vocab_dict, glove_dict):
    lookup_table = torch.empty((len(vocab_dict)+2, 50))
    for token_id in sorted(vocab_dict.values()):
        if token_id in glove_dict:
            lookup_table[token_id] = glove_dict[token_id]
        else:
            lookup_table[token_id] = torch.ones((1, 50))  # For unknown tokens (we change it to ones due to the packed sequence)
    lookup_table[0] = torch.zeros((1, 50))
    return lookup_table

# %% -------------------------------------- LSTM Class -----------------------------------------------------------------
class SentimentLSTM(nn.Module):
    def __init__(self, vocab_size, hidden_size=args.hidden_size, n_layers=args.n_layers):
        super(SentimentLSTM, self).__init__()
        self.embedding = nn.Embedding(vocab_size + 2, args.embedding_dim, padding_idx=0)
        # padding_idx=0 makes the embedding table assign a vector of zeros to the padded 0s, i.e, this embedding is not learnt
        self.lstm = nn.LSTM(input_size=args.embedding_dim, hidden_size=hidden_size, num_layers=n_layers, dropout=args.lstm_drop)
        # This layer will act as a learnable weighted average over time of all the outputs of the LSTM along the input
        self.mean = nn.Linear(args.seq_len*hidden_size, hidden_size)  # sequence, i.e, the features for each word
        self.bn_mean = nn.BatchNorm1d(hidden_size)
        self.out = nn.Linear(hidden_size, 2)  # This final layer maps this average to classify positive/negative reviews
        self.drop = nn.Dropout(args.lin_drop)

    def forward(self, x):
        # The output of embedding is (batch, seq_len, embedding_dim) but we want shape (seq_len, batch, embedding_dim)
        x = self.embedding(x).permute(1, 0, 2)  # to input to our LSTM
        if args.use_packed_sequence:
            lenghts = []  # Gets a list of the actual length of each sentence before padding. This is definitely better done
            for sentence_idx in range(x.shape[1]):  # at the preprocessing step, but I did not want to change it too much
                n_zeros = torch.sum(x[:, sentence_idx, :] == 0) / 50
                lenghts.append(args.seq_len - n_zeros.item())
            # Converts the input padded batched tensor into a packed sequence so that the 0s will be ignored
            x = nn.utils.rnn.pack_padded_sequence(x, lenghts, enforce_sorted=False)
        lstm_out, _ = self.lstm(x)  # Note that in this case (compared to 1_ChirpApprox), we completely forget
        # about h_state and c_state because it makes no sense to use an stateful LSTM, i.e, it makes no sense to pass
        # the memory of previous batches to the next batches, as they are not related at all
        if args.use_packed_sequence:
            # Converts back to the padded tensor, we use total_length=args.seq_len to be able to use a linear layer after this
            lstm_out, _ = nn.utils.rnn.pad_packed_sequence(lstm_out, total_length=args.seq_len)
        mean_over_t = self.drop(self.bn_mean(self.mean(lstm_out.permute(1, 0, 2).reshape(lstm_out.shape[1], -1))))
        return self.out(mean_over_t)

# %% -------------------------------------- Data Prep ------------------------------------------------------------------
""" Using cleaned data """
train_data = pd.read_csv('train_cleaned.csv')
val_data = pd.read_csv('val_cleaned.csv')
test_data = pd.read_csv('test_cleaned.csv')

x_train_raw, y_train = train_data["cleaned_text"].values, torch.LongTensor(train_data["Label"].values).to(device)

x_val_raw, y_val = val_data["cleaned_text"].values, torch.LongTensor(val_data["Label"].values).to(device)

x_dev_raw, y_dev = test_data["cleaned_text"].values, torch.LongTensor(test_data["Label"].values).to(device)

print("Tokenizing all the examples to get a vocab dict and the maximum sequence length...")
token_ids, msl = extract_vocab_dict_and_msl(x_train_raw, x_dev_raw, x_val_raw)

if args.seq_len == "get_max_from_data":
    args.seq_len = msl
del train_data, test_data, val_data

glove_embeddings = get_glove_embeddings(token_ids)

print("Converting all the sentences to sequences of token ids...")
x_train = convert_to_ids(x_train_raw, token_ids, args.seq_len)
x_dev = convert_to_ids(x_dev_raw, token_ids, args.seq_len)
x_val = convert_to_ids(x_val_raw, token_ids, args.seq_len)

del x_train_raw, x_dev_raw, x_val_raw

x_train, x_dev, x_val = torch.LongTensor(x_train).to(device), torch.LongTensor(x_dev).to(device), torch.LongTensor(x_val).to(device)

# %% -------------------------------------- Training Prep ----------------------------------------------------------
model = SentimentLSTM(len(token_ids)).to(device)
look_up_table = get_glove_table(token_ids, glove_embeddings)
model.embedding.weight.data.copy_(look_up_table)
optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
criterion = nn.CrossEntropyLoss()

# %% -------------------------------------- Training Loop ----------------------------------------------------------
labels_ditrib = torch.unique(y_dev, return_counts=True)
print("The no information rate is {:.2f}".format(100*labels_ditrib[1].max().item()/len(y_dev)))
if TRAIN:
    acc_dev_best = 0
    print("Starting training loop...")
    for epoch in range(args.n_epochs):

        loss_train, train_steps = 0, 0
        model.train()
        total = len(x_train) // args.batch_size #(total number of batches - 1)
        with tqdm(total=total, desc="Epoch {}".format(epoch)) as pbar:
            for batch in range(len(x_train)//args.batch_size + 1):
                inds = slice(batch*args.batch_size, (batch+1)*args.batch_size)
                optimizer.zero_grad()
                logits = model(x_train[inds])
                loss = criterion(logits, y_train[inds])
                loss.backward()
                optimizer.step()
                loss_train += loss.item()
                train_steps += 1
                pbar.update(1)
                pbar.set_postfix_str("Training Loss: {:.5f}".format(loss_train / train_steps))

        acc_train = acc(x_train, y_train)   # calculate accuracy using this function

        y_val_pred = acc(x_val, y_val, return_labels=True)  # calculate predicted output labels using the same function
        acc_val = 100*accuracy_score(y_val.cpu().numpy(), y_val_pred)   # by changing return_labels to True

        print("Epoch {} | Train Loss {:.5f}, Train Acc {:.2f}, Val Acc {:.2f}".format(
            epoch, loss_train / train_steps, acc_train, acc_val))

        if acc_val > acc_dev_best and SAVE_MODEL:
            acc_dev_best = acc_val
            torch.save(model.state_dict(), "lstm_sentiment_cleanData.pt")
            print("The model has been saved!")

# %% ------------------------------------------ Final test -------------------------------------------------------------
model.load_state_dict(torch.load("lstm_sentiment_cleanData.pt"))
model.eval()
y_test_pred = acc(x_dev, y_dev, return_labels=True)
print("The accuracy on the test set is {:.2f}".format(100*accuracy_score(y_dev.cpu().numpy(), y_test_pred), "%"))
print("The confusion matrix is")
print(confusion_matrix(y_dev.cpu().numpy(), y_test_pred))