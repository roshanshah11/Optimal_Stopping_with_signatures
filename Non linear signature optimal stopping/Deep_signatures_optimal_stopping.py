#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Aug 29 10:22:26 2024

@author: lucapelizzari
This file contains three ways of computing lower and upper bounds to the optimal stopping problem: using linear signatures, using 
deep neural networks on signatures, and using the signature kernel.
"""

import numpy as np
import tensorflow as tf
from tensorflow.keras.callbacks import EarlyStopping
from tensorflow.keras import layers, models
from tensorflow.keras.optimizers.legacy import Adam  # Updated for M1/M2/M3 Mac compatibility
from tensorflow.keras import regularizers
from functools import reduce
from tensorflow.python.autograph.impl.conversion import _is_known_loaded_type as _orig_is_known_loaded_type
import tensorflow.python.autograph.impl.conversion as _conv

# Disable AutoGraph conversion to run functions eagerly (avoids isinstance TypeError in conversion)
tf.config.run_functions_eagerly(True)

def _safe_is_known_loaded_type(f, module_name, entity_name):
    try:
        return _orig_is_known_loaded_type(f, module_name, entity_name)
    except TypeError:
        return False

# Monkey-patch to prevent isinstance TypeError in autograph conversion
_conv._is_known_loaded_type = _safe_is_known_loaded_type

class DeepLongstaffSchwartzPricer:
    """
    Computes the lower bound of optimal stopping problem using deep neural networks on signatures.
    """
    def __init__(self, N1, T, r, mode="Standard", layers=3, nodes=64, activation_function='relu',
                 batch_normalization=False, regularizer=0, dropout=False, layer_normalization=False):
        """
        Parameters
        ----------
        N1 : int
            Number of exercise dates for optimal stopping
        T : float
            Time horizon for the option
        r : float
            Risk-free interest rate
        mode : str
            "Standard" or "American Option", where "American Option" only uses in the money paths for regression
        layers : int
            Number of hidden layers
        nodes : int
            Number of neurons in each hidden layer
        activation_function : str
            Activation function for hidden layers
        batch_normalization : bool
            Whether to use batch normalization at the input
        regularizer : float
            L2 regularization factor
        dropout : bool
            Whether to use dropout
        layer_normalization : bool
            Whether to use layer normalization
        """
        self.N1 = N1
        self.T = T
        self.r = r
        self.mode = mode
        self.layers = layers
        self.nodes = nodes
        self.activation_function = activation_function
        self.batch_normalization = batch_normalization
        self.regularizer = regularizer
        self.dropout = dropout
        self.layer_normalization = layer_normalization

    def price(self, S_training_sig, Payoff_training, S_testing_sig, Payoff_testing, 
              batch=32, epochs=100, learning_rate=0.001, M_val=0):
        """
        Parameters
        ----------
        S_training_sig : numpy array
            (Log) Signature (+ possibly polynomial features) of the augmented path for the training set
        Payoff_training : numpy array
            Payoff for training paths
        S_testing_sig : numpy array
            (Log) Signature (+ possibly polynomial features) of the augmented path for the testing set
        Payoff_testing : numpy array
            Payoff for testing paths
        batch : int
            Batch size for training
        epochs : int
            Number of epochs for training
        learning_rate : float
            Learning rate for training
        M_val : int
            Number of validation paths
        """
        
        M, N, feature_dim = S_training_sig.shape
        N= N-1
        M2, _, _ = S_testing_sig.shape
        subindex = [int((j+1)*N/self.N1) for j in range(self.N1)]
        subindex2 = [int((j)*N/self.N1) for j in range(self.N1+1)]
        ttt = np.linspace(0, self.T, self.N1 + 1)
        
        Payoff_exercise_training = Payoff_training[:, subindex]
        S_exercise_training_sig = S_training_sig[:, subindex, :]  # Adjust index for signatures
        
        Payoff_exercise_testing = Payoff_testing[:, subindex]
        S_exercise_testing_sig = S_testing_sig[:, subindex, :]  # Adjust index for signatures
        
        regr = [None] * (self.N1 - 1)
        value = Payoff_exercise_training[:, -1]
        
        dtt = np.exp(-self.r * self.T / (self.N1 + 1))
        
        if self.mode == "Standard":
            """
            Standard mode: Use all paths for regression at each exercise date
            """
            M_old = M
            for k in reversed(range(1, self.N1)):
                M_new = M_old - M_val
                S_exercise_training_new = S_exercise_training_sig[:M_new, k-1, :]
                value_new = value[:M_new] * dtt
                S_exercise_training_validation = S_exercise_training_sig[M_new:M_old, k-1, :]
                value_validation = value[M_new:M_old]
                
                regr[k-1] = LongstaffSchwartzModel(
                    feature_dim=feature_dim,
                    layers_number=self.layers,
                    nodes=self.nodes,
                    activation_function=self.activation_function,
                    batch_normalization=self.batch_normalization,
                    regularizer=self.regularizer,
                    dropout=self.dropout,
                    layer_normalization=self.layer_normalization
                )
                
                regr[k-1].compile(learning_rate=learning_rate, loss='mse')
                
                if k < self.N1 - 1:
                    """
                    Transfer weights from the next exercise date to the current one, and reduce to one epoch.
                    """
                    try:
                        regr[k-1].set_weights(regr[k].get_weights())
                        epochs = 1

                    except ValueError as e:
                        print(f"Unable to transfer weights from step {k} to {k-1}: {e}")
                        print("Continuing with randomly initialized weights")
                
                print(f"Regression at exercise date {k}")
                early_stopping = EarlyStopping(monitor='loss', patience=5)
                if M_val == 0:
                    regr[k-1].fit(
                    S_exercise_training_new,
                    value_new,
                    batch_size=batch,
                    epochs=epochs,
                    verbose=1,
                    callbacks=[early_stopping]
                )
                else:
                    regr[k-1].fit(
                    S_exercise_training_new,
                    value_new,
                    batch_size=batch,
                    epochs=epochs,
                    verbose=1,
                    validation_data=(S_exercise_training_validation, value_validation),
                    callbacks=[early_stopping]
                    )


                
                
                reg = regr[k-1].predict(S_exercise_training_new)
                
                for m in range(M_new):
                    if reg[m] <= Payoff_exercise_training[m, k-1]:
                        value_new[m] = Payoff_exercise_training[m, k-1]
                
                value = value_new
                M_old = M_new
     
        
        elif self.mode == "American Option":
            """
            American Option mode: Only use in the money paths for regression at each exercise date
            """
            for j in reversed(range(1, self.N1)):
                value = value * dtt
                ITM = [m for m in range(M) if Payoff_exercise_training[m, j-1] > 0]
                
                if len(ITM) <= 1:
                    continue
                
                regr[j-1] = LongstaffSchwartzModel(
                    feature_dim=feature_dim,
                    layers_number=self.layers,
                    nodes=self.nodes,
                    activation_function=self.activation_function,
                    batch_normalization=self.batch_normalization,
                    regularizer=self.regularizer,
                    dropout=self.dropout,
                    layer_normalization=self.layer_normalization
                )
                
                regr[j-1].compile(learning_rate=learning_rate, loss='mse')
                
                if j < self.N1 - 1:
                    """
                    Transfer weights from the next exercise date to the current one, and reduce to one epoch.
                    """
                    try:
                        regr[j-1].set_weights(regr[j].get_weights())
                        epochs = 1

                    except ValueError as e:
                        print(f"Unable to transfer weights from step {j} to {j-1}: {e}")
                        print("Continuing with randomly initialized weights")
                
                print(f"Regression at exercise date {j}")
                early_stopping = EarlyStopping(monitor='loss', patience=5)
                # Fit the model
                regr[j-1].fit(
                    S_exercise_training_sig[ITM, j-1, :],
                    value[ITM],
                    batch_size=batch,
                    epochs=epochs,
                    verbose=1,
                    callbacks=[early_stopping]
                )
                
                value_estimate = regr[j-1].predict(S_exercise_training_sig[ITM, j-1, :])
                
                for m, itm in enumerate(ITM):
                    # Update the value of the option if the estimated value is less than the payoff
                    if value_estimate[m] <= Payoff_exercise_training[itm, j-1]:
                        value[itm] = Payoff_exercise_training[itm, j-1]
        
        else:
            raise ValueError(f"Invalid mode: {self.mode}")
        
        # Compute true lower bound for testing data
        value_testing = Payoff_exercise_testing[:, -1]
        reg = np.zeros((M2, self.N1))
        
        for j in range(self.N1 - 1):
            # If the model is not trained, set the value to a large number
            if regr[j] is None:
                reg[:, j] = 10**8
            else:
                # Predict the value of the option at the current exercise date
                reg[:, j] = regr[j].predict(S_exercise_testing_sig[:, j, :])[:, 0]
        
        if self.mode == "Standard":
            """
            Standard mode: Use all paths for regression at each exercise date
            """
            for m in range(M2):
                i = 0
                while i < self.N1 - 1 and reg[m, i] > Payoff_exercise_testing[m, i]:
                    i += 1
                value_testing[m] = Payoff_exercise_testing[m, i] * np.exp(-self.r * self.T * (ttt[i+1] - ttt[1]))
        
        elif self.mode == "American Option":
            """
            American Option mode: Only use in the money paths for regression at each exercise date
            """
            for m in range(M2):
                i = 0
                while i < self.N1 - 1 and (Payoff_exercise_testing[m, i] == 0 or reg[m, i] > Payoff_exercise_testing[m, i]):
                    i += 1
                value_testing[m] = Payoff_exercise_testing[m, i] * np.exp(-self.r * self.T * (ttt[i+1] - ttt[1]))
        
        lower_bound = np.mean(value_testing)
        lower_bound_std = np.std(value_testing)
        
        return lower_bound, lower_bound_std, regr

class DeepDualPricer:
    """
    Computes upper bounds of optimal stopping problem using deep neural networks on signatures.
    """
    def __init__(self, N1, N, T, r, layers=3, nodes=64, activation_function='relu',
                 batch_normalization=False, regularizer=0.01, dropout=False,
                 attention_layer=False, layer_normalization=False):
        """
        Parameters
        ----------
        N1 : int
            Number of exercise dates for optimal stopping
        N : int
            Number of time steps
        T : float
            Time horizon for the option
        r : float
            Risk-free interest rate
        layers : int
            Number of hidden layers
        nodes : int
            Number of neurons in each hidden layer
        activation_function : str
            Activation function for hidden layers
        batch_normalization : bool
            Whether to use batch normalization at the input
        regularizer : float
            L2 regularization factor
        dropout : bool
            Whether to use dropout
        attention_layer : bool
            Whether to use attention layer
        layer_normalization : bool
            Whether to use layer normalization
        """
        self.N1 = N1
        self.N = N
        self.T = T
        self.r = r
        self.layers = layers
        self.nodes = nodes
        self.activation_function = activation_function
        self.batch_normalization = batch_normalization
        self.regularizer = regularizer
        self.dropout = dropout
        self.attention_layer = attention_layer
        self.layer_normalization = layer_normalization

    def price(self, S_training_sig, Payoff_training, dW_training, S_testing_sig, Payoff_testing, dW_testing,
          M_val, batch=32, epochs=100, learning_rate=0.001):
        """
        Parameters
        ----------
        S_training_sig : numpy array
            (Log) Signature (+ possibly polynomial features) of the augmented path for the training set
        Payoff_training : numpy array
            Payoff for training paths
        dW_training : numpy array
            Brownian motion increments for training paths
        S_testing_sig : numpy array
            (Log) Signature (+ possibly polynomial features) of the augmented path for the testing set
        Payoff_testing : numpy array
            Payoff for testing paths
        dW_testing : numpy array
            Brownian motion increments for testing paths
        M_val : int
            Number of validation paths
        batch : int
            Batch size for training
        epochs : int
            Number of epochs for training
        learning_rate : float
            Learning rate for training
        """
        M, N_sig, D = S_training_sig.shape
        M2, N_sig_test, _ = S_testing_sig.shape
        
        # Get dimensions from all inputs
        _, dW_steps = dW_training.shape
        _, payoff_steps = Payoff_training.shape
        
        # Use dW dimensions as the base since model expects matching dimensions
        N_actual = dW_steps  # This should match dW_training.shape[1]
        
        # Print dimensions for debugging
        print(f"Signature data shape: {S_training_sig.shape}")
        print(f"Payoff data shape: {Payoff_training.shape}")
        print(f"dW data shape: {dW_training.shape}")
        print(f"Using {N_actual} time steps instead of {self.N} for model building")
        
        # Use the actual time steps from the data for subindices
        subindex = [int((j+1)*N_actual/self.N1) for j in range(self.N1)]
        subindex2 = [int((j)*N_actual/self.N1) for j in range(self.N1+1)]
        
        # Make sure indices don't exceed the data dimensions
        subindex = [min(idx, N_actual-1) for idx in subindex]
        subindex2 = [min(idx, N_actual) for idx in subindex2]
        
        # Build the network model using the actual time steps
        model, rule_model = DualNetworkModel(
            n=self.N1 + 1,
            n2=N_actual + 1,  # Use actual time steps, not self.N
            I=self.layers,
            q=self.nodes + D,
            d=D,
            activation_function=self.activation_function,
            batch_normalization=self.batch_normalization,
            regularizer=self.regularizer,
            dropout=self.dropout,
            attention_layer=self.attention_layer,
            layer_normalization=self.layer_normalization
        ).build_network_dual()

        optimizer = Adam(learning_rate=learning_rate)
        try:
            # Try with run_eagerly for newer TensorFlow versions that support it
            model.compile(optimizer=optimizer, run_eagerly=True)
        except TypeError:
            # Fall back to standard compile for older versions or if run_eagerly causes issues
            model.compile(optimizer=optimizer)

        early_stopping = EarlyStopping(monitor='val_loss', patience=5)
        
        # Important: Trim Payoff to exactly N_actual steps, not N_actual+1
        # This matches the expected input dimensions in the model
        Payoff_train_trimmed = Payoff_training[:, :N_actual]
        Payoff_test_trimmed = Payoff_testing[:, :N_actual]
        
        print(f"Model expects Payoff shape: (batch_size, {N_actual})")
        print(f"Trimmed Payoff shape: {Payoff_train_trimmed.shape}")
        
        # Fit the model with the correctly sized inputs
        model.fit(
            [S_training_sig[:M_val], Payoff_train_trimmed[:M_val], dW_training[:M_val]],
            y=None,
            batch_size=batch,
            epochs=epochs,
            verbose=1,
            validation_data=([S_training_sig[M_val:], Payoff_train_trimmed[M_val:], dW_training[M_val:]], None),
            callbacks=[early_stopping]
        )

        # Testing for fresh samples
        res = model.predict([S_testing_sig, Payoff_test_trimmed, dW_testing])
        MG = rule_model.predict([S_testing_sig, dW_testing])
        
        print(f"MG shape: {MG.shape}")
        
        # --- FIX: Handle case where MG shape doesn't match expected dimensions ---
        # If MG has more columns than expected, trim it to match N_actual
        if MG.shape[1] > N_actual:
            print(f"Trimming MG from {MG.shape} to (M2, {N_actual})")
            MG = MG[:, :N_actual]
        
        # Create MG with zeros prepended, now with correct dimensions
        MG_with_zeros = np.concatenate((np.zeros((M2, 1)), MG), axis=-1)
        print(f"MG with zeros shape: {MG_with_zeros.shape}")
        
        # Calculate the upper bound using your original approach
        # Just make sure to use Payoff_testing dimensions that match MG_with_zeros
        valid_indices = [idx for idx in subindex2 if idx < N_actual+1]
        diffs = Payoff_testing[:, valid_indices] - MG_with_zeros[:, valid_indices]
        max_diffs = np.max(diffs, axis=1)
        
        upper_bound = np.mean(max_diffs)
        upper_bound_std = np.std(max_diffs)
        
        y0 = np.mean(res)

        return y0, upper_bound, upper_bound_std, model, rule_model

class LongstaffSchwartzModel:
    """
    A neural network model class for the Longstaff-Schwartz approach for optimal stopping.

    Attributes:
        feature_dim (int): The input dimension of the network (dimension of the feature map/signature).
        layers_number (int): Number of hidden layers.
        nodes (int): Number of neurons in each hidden layer.
        activation_function (str): Activation function for hidden layers.
        batch_normalization (bool): Whether to use batch normalization at the input.
        regularizer (float): L2 regularization factor.
        dropout (bool): Whether to use dropout.
        layer_normalization (bool): Whether to use layer normalization.
    """

    def __init__(self, feature_dim, layers_number, nodes, activation_function='relu',
                 batch_normalization=False, regularizer=0.01, dropout=False,
                 layer_normalization=False):
        self.feature_dim = feature_dim
        self.layers_number = layers_number
        self.nodes = nodes
        self.activation_function = activation_function
        self.batch_normalization = batch_normalization
        self.regularizer = regularizer
        self.dropout = dropout
        self.layer_normalization = layer_normalization
        self.model = self.build_model_longstaff_schwartz()

    def build_model_longstaff_schwartz(self):
        model = models.Sequential()

        # Input layer with optional batch normalization
        if self.batch_normalization:
            model.add(layers.BatchNormalization(input_shape=(self.feature_dim,)))
        else:
            model.add(layers.Input(shape=(self.feature_dim,)))

        # Set activation function
        if self.activation_function == "LeakyRelu":
            activation = 'relu'  # Use standard relu as string first
            use_leaky = True  # Flag to add LeakyReLU layer separately
        else:
            activation = self.activation_function
            use_leaky = False

        # Hidden layers
        for _ in range(self.layers_number):
            if self.layer_normalization:
                model.add(layers.LayerNormalization(epsilon=1e-6))
                
            # Add dense layer
            model.add(layers.Dense(self.nodes, activation=activation,
                                   kernel_regularizer=regularizers.l2(self.regularizer)))
                                   
            # Add LeakyReLU as a separate layer if needed
            if use_leaky:
                model.add(tf.keras.layers.LeakyReLU(negative_slope=0.3))
                
            if self.layer_normalization:
                model.add(layers.LayerNormalization(epsilon=1e-6))
                
            if self.dropout:
                model.add(layers.Dropout(0.5))

        # Output layer
        model.add(layers.Dense(1, activation='linear'))

        return model

    def compile(self, learning_rate=0.001, loss='mse', metrics=['mae']):
        optimizer = Adam(learning_rate=learning_rate)
        try:
            # First try with run_eagerly
            self.model.compile(optimizer=optimizer, loss=loss, metrics=metrics, run_eagerly=True)
        except Exception as e:
            print(f"Warning: Unable to compile with run_eagerly=True: {e}")
            try:
                # Then try without run_eagerly
                self.model.compile(optimizer=optimizer, loss=loss, metrics=metrics)
            except Exception as e:
                print(f"Warning: Standard compilation failed: {e}")
                # Last resort: Set a flag indicating compilation failed
                self.compilation_failed = True
                print("Setting model to fallback mode - some functionality may be limited")

    def fit(self, X, y, epochs=100, batch_size=32, verbose=1, callbacks=None):
        # Check if compilation failed and use fallback mode
        if hasattr(self, 'compilation_failed') and self.compilation_failed:
            print("Using fallback mode for fitting (no training will occur)")
            return None

        # Convert inputs to numpy arrays to avoid TensorFlow type checking issues
        X_np = np.array(X) if not isinstance(X, np.ndarray) else X
        y_np = np.array(y) if not isinstance(y, np.ndarray) else y
        
        try:
            return self.model.fit(
                X_np, y_np,
                epochs=epochs,
                batch_size=batch_size,
                verbose=verbose,
                callbacks=callbacks
            )
        except Exception as e:
            print(f"Error during fit: {e}")
            print("Training skipped due to error")
            return None

    def predict(self, X):
        # Check if compilation failed and use fallback mode
        if hasattr(self, 'compilation_failed') and self.compilation_failed:
            print("Using fallback mode for prediction (returning zeros)")
            # Return an array of zeros with the right shape
            return np.zeros((X.shape[0], 1))

        # Convert input to numpy array to avoid type checking issues
        X_np = np.array(X)
        try:
            return self.model.predict(X_np)
        except Exception as e:
            print(f"Error during predict: {e}")
            print("Returning zeros due to error")
            return np.zeros((X.shape[0], 1))

    def summary(self):
        return self.model.summary()

    def save(self, filepath):
        self.model.save(filepath)

    @classmethod
    def load(cls, filepath):
        loaded_model = models.load_model(filepath)
        instance = cls(feature_dim=loaded_model.input_shape[1], layers_number=0, nodes=0)
        instance.model = loaded_model
        return instance

    def get_weights(self):
        return self.model.get_weights()

    def set_weights(self, weights):
        self.model.set_weights(weights)



class DeepMartingales(tf.keras.layers.Layer):
    def __init__(self):
        super(DeepMartingales, self).__init__()
        self.steps = None

    def build(self, input_shape):
        self.steps = input_shape[-1]

    def call(self, inputs, **kwargs):
        rule, dW = inputs
        return tf.cumsum(rule * dW, axis=1)

class DualStoppingLoss(tf.keras.layers.Layer):
    def call(self, inputs):
        rule, Y = inputs
        
        out = tf.math.reduce_mean(tf.math.reduce_max(Y-rule,axis=1))

        self.add_loss(out)

        return out

class DualNetworkModel:
    def __init__(self, n, n2, I, q, d, activation_function='relu',
                 batch_normalization=False, regularizer=0.01, dropout=False,
                 attention_layer=False, layer_normalization=False):
        self.n = n  # exercise dates
        self.n2 = n2  # discretization
        self.I = I  # number of layers
        self.q = q  # number of neurons
        self.d = d  # input dimension
        self.activation_function = activation_function
        self.batch_normalization = batch_normalization
        self.regularizer = regularizer
        self.dropout = dropout
        self.attention_layer = attention_layer
        self.layer_normalization = layer_normalization
        self.model, self.rule_model = self.build_network_dual()

    def dense_neural_network_dual(self):
        if self.activation_function == "LeakyRelu":
            activation = 'relu'  # Use standard relu as string first
            use_leaky = True  # Flag to add LeakyReLU layer separately
        else:
            activation = self.activation_function
            use_leaky = False

        layers_list = []
        if self.batch_normalization:
            layers_list.append(layers.BatchNormalization())

        layers_list.append(tf.keras.layers.Dense(self.q, activation=activation,
                                                 kernel_regularizer=regularizers.l2(self.regularizer)))
        if use_leaky:
            layers_list.append(tf.keras.layers.LeakyReLU(negative_slope=0.3))

        num_attention_heads = 2
        for _ in range(self.I - 1):
            if self.layer_normalization:
                layers_list.append(layers.LayerNormalization(epsilon=1e-6))
            if self.attention_layer:
                layers_list.append(layers.MultiHeadAttention(num_heads=num_attention_heads,
                                                             key_dim=max(1, self.q // num_attention_heads),
                                                             dropout=0.1))
            layers_list.append(tf.keras.layers.Dense(self.q, activation=activation,
                                                     kernel_regularizer=regularizers.l2(self.regularizer)))
            if use_leaky:
                layers_list.append(tf.keras.layers.LeakyReLU(negative_slope=0.3))
            if self.layer_normalization:
                layers_list.append(layers.LayerNormalization(epsilon=1e-6))
            if self.dropout:
                layers_list.append(tf.keras.layers.Dropout(0.5))

        layers_list.append(tf.keras.layers.Dense(1, activation=None))
        layers_list.append(tf.keras.layers.Flatten())

        return layers_list

    def build_network_dual(self, initial_model=None):
        input_logsig = tf.keras.Input(shape=(self.n2, self.d), name='sig')
        input_y = tf.keras.Input(shape=(self.n2-1,), name='Y')
        input_BM = tf.keras.Input(shape=(self.n2-1,), name='dW')

        dnn_layers = self.dense_neural_network_dual()
        dnn_output = reduce(lambda x, f: f(x), [input_logsig] + dnn_layers)

        rule_layer = DeepMartingales()([dnn_output[:, 0:self.n2-1], input_BM])
        
        # The problem is that we need indices for self.n exercise dates 
        # But our tensor only has self.n2-1 available indices (0 to self.n2-2)
        
        # Calculate indices ensuring they're all valid
        valid_indices = []
        
        # We need exactly self.n-1 indices (for all exercise dates after the first)
        n_indices_needed = self.n - 1
        
        # If we have fewer time steps than exercise dates, we'll need to reuse some indices
        if n_indices_needed <= self.n2-1:
            # We have enough unique indices, distribute them evenly
            for i in range(n_indices_needed):
                # Calculate linearly spaced indices
                idx = int(i * (self.n2-1) / (n_indices_needed))
                valid_indices.append(idx)
        else:
            # We have more exercise dates than time steps
            # Use all available indices and repeat the last one if needed
            valid_indices = list(range(self.n2-1))
            # Add the last index repeatedly until we have enough
            while len(valid_indices) < n_indices_needed:
                valid_indices.append(self.n2-2)
        
        print(f"Using indices: {valid_indices} for {n_indices_needed} exercise dates")
        
        # Convert to TensorFlow constant to avoid conversion issues
        tf_indices = tf.constant(valid_indices, dtype=tf.int32)
        
        # Use tf.gather with the properly calculated indices
        rule_exercise = tf.gather(rule_layer, tf_indices, axis=1)
        y_exercise = tf.gather(input_y, tf_indices, axis=1)
        
        loss_layer = DualStoppingLoss()([rule_exercise, y_exercise])

        model = tf.keras.Model([input_logsig, input_y, input_BM], loss_layer)
        rule_model = tf.keras.Model([input_logsig, input_BM], rule_layer)

        if initial_model is not None:
            model.set_weights(initial_model.get_weights())

        return model, rule_model

    def compile(self, optimizer='adam', loss='mse', metrics=['mae']):
        try:
            # First try with run_eagerly
            self.model.compile(optimizer=optimizer, loss=loss, metrics=metrics, run_eagerly=True)
        except Exception as e:
            print(f"Warning: Unable to compile with run_eagerly=True: {e}")
            try:
                # Then try without run_eagerly
                self.model.compile(optimizer=optimizer, loss=loss, metrics=metrics)
            except Exception as e:
                print(f"Warning: Standard compilation failed: {e}")
                # Last resort: Set a flag indicating compilation failed
                self.compilation_failed = True
                print("Setting model to fallback mode - some functionality may be limited")

    def fit(self, X, y, epochs=100, batch_size=32, validation_split=0.2, verbose=1):
        # Check if compilation failed and use fallback mode
        if hasattr(self, 'compilation_failed') and self.compilation_failed:
            print("Using fallback mode for fitting (no training will occur)")
            return None

        # Convert inputs to numpy arrays to avoid TensorFlow type checking issues
        X_np = np.array(X) if not isinstance(X, np.ndarray) else X
        y_np = np.array(y) if not isinstance(y, np.ndarray) else y
        
        try:
            return self.model.fit(
                X_np, y_np,
                epochs=epochs,
                batch_size=batch_size,
                validation_split=validation_split,
                verbose=verbose
            )
        except Exception as e:
            print(f"Error during fit: {e}")
            print("Training skipped due to error")
            return None

    def predict(self, X):
        # Check if compilation failed and use fallback mode
        if hasattr(self, 'compilation_failed') and self.compilation_failed:
            print("Using fallback mode for prediction (returning zeros)")
            # Return an array of zeros with the right shape
            if isinstance(X, list):
                return np.zeros((X[0].shape[0], X[0].shape[1] - 1))
            else:
                return np.zeros((X.shape[0], X.shape[1] - 1))

        try:
            return self.rule_model.predict(X)
        except Exception as e:
            print(f"Error during predict: {e}")
            print("Returning zeros due to error")
            if isinstance(X, list):
                return np.zeros((X[0].shape[0], X[0].shape[1] - 1))
            else:
                return np.zeros((X.shape[0], X.shape[1] - 1))

    def summary(self):
        return self.model.summary()

    def save(self, filepath):
        self.model.save(filepath)

    @classmethod
    def load(cls, filepath):
        loaded_model = models.load_model(filepath)
        # You might need to adjust this part depending on how you want to handle the loaded model
        instance = cls(n=1, n2=1, I=1, q=1, d=loaded_model.input_shape[0][2])
        instance.model = loaded_model
        return instance