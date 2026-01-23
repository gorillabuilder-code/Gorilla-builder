import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const port = process.env.PORT || 3000;

// Serve static assets (the built React app)
app.use('/static', express.static(path.join(__dirname, 'static')));

// Serve the 'src' folder (optional, for debugging if needed)
app.use('/src', express.static(path.join(__dirname, 'src')));

// SPA Fallback: Serve index.html for any unknown route
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'index.html'));
});

app.listen(port, () => {
  console.log(`Gorilla Server running on port ${port}`);
});